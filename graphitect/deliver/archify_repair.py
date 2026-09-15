"""Agent-driven layout repair for archify diagrams.

archify_adapter's grid layout is a simple topological-depth heuristic with
no real connection routing - fine for small graphs, but archify's own
strict layout validator rejects dense real-world graphs (edges crossing
unrelated components, labels overlapping boxes - confirmed live against
FundersAI's real 7,885-node graph). Rather than hand-writing a real graph
layout/routing algorithm, this asks an LLM to patch the IR's own
routing/label-position fields (fromSide/toSide/route/via/labelAt/labelDx/
labelDy/labelSegment) against archify's exact structured diagnostics
(`archify deliver --json`'s `diagnostics[]`: code, offending connection,
obstacle, and often a concrete suggested fix), then re-validates - the
"agent iterates against archify validate feedback" approach flagged as
future work in plan.md, built as an LLM-backend loop (reusing the same
Ollama/Anthropic/Gemini backends as Synthesize) so it works from the
standalone CLI too, not only inside an agent host.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..synthesize.llm_backend import LLMBackend
from .archify_ir import ArchitectureIR

_REPAIR_TOOL_NAME = "emit_repaired_architecture_ir"


class RepairUnavailableError(RuntimeError):
    """The optional LLM layout-repair step could not produce a valid IR."""

_REPAIR_SYSTEM_PROMPT = """\
You are repairing an archify architecture diagram IR that failed archify's
own layout validator. You will be given the current IR (JSON) and the exact
list of validator diagnostics (each with a code, the offending connection or
label, the obstacle it collides with, and often a suggested fix).

Fix ONLY layout/routing/label-position fields on the existing connections:
fromSide, toSide, route ("auto"|"straight"|"orthogonal-h"|"orthogonal-v"),
via (a list of [x, y] waypoints), labelAt ([x, y]), labelDx, labelDy,
labelSegment. You may also adjust a component's row/col/pos/size if a
diagnostic specifically calls for moving it. Do NOT change any id, label,
from/to endpoint, or add/remove any component or connection - every
diagnostic must be resolved by adjusting geometry, not by altering what the
diagram says. When a diagnostic's message includes a "Suggested fix" with
concrete coordinates, prefer using those exact numbers over estimating your
own.

Return the complete corrected IR as a single JSON object matching the given
schema exactly - every field from the input IR must still be present unless
you are deliberately changing it.
"""


def run_deliver_json(
    bin_cmd: list[str], ir_path: Path, out_path: Path, *, diagram_type: str = "architecture"
) -> dict:
    """Runs `archify deliver ... --json` and returns its parsed JSON report
    regardless of exit code. Confirmed live: archify's own --json output
    carries structured `diagnostics` (code/subject/evidence/supportedFixes)
    on failure - the identical shape `archify validate --json` gives - so a
    single deliver attempt is enough to both try rendering and get repair
    feedback if it fails, with no separate validate call needed.

    Not underscore-prefixed: also used by archify_adapter.render()'s
    no-LLM-backend mechanical label-fix retry (_apply_suggested_label_fixes)
    - both need the exact same structured diagnostics, LLM-driven repair or
    not.
    """
    result = subprocess.run(
        [*bin_cmd, "deliver", diagram_type, str(ir_path), str(out_path), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"archify deliver --json produced non-JSON output (exit {result.returncode}): "
            f"stdout={result.stdout[:500]!r} stderr={result.stderr[:500]!r}"
        ) from exc


def repair_and_deliver(
    ir: ArchitectureIR,
    bin_cmd: list[str],
    ir_path: Path,
    out_path: Path,
    backend: LLMBackend,
    *,
    max_iterations: int = 3,
) -> dict:
    """Writes `ir`, attempts `archify deliver`, and if archify's validator
    rejects it, asks `backend` to patch the IR against the real diagnostics
    and retries - up to `max_iterations` total delivery attempts. Returns
    the final JSON report (whatever the last attempt produced, success or
    failure) - the caller decides what to do with a still-failing report.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")

    schema = ArchitectureIR.model_json_schema()

    for attempt in range(1, max_iterations + 1):
        ir_path.write_text(json.dumps(ir.model_dump_archify(), indent=2), encoding="utf-8")
        report = run_deliver_json(bin_cmd, ir_path, out_path)
        if report.get("ok"):
            return report

        diagnostics = report.get("diagnostics") or []
        if not diagnostics or attempt == max_iterations:
            return report

        user_prompt = (
            f"Current IR:\n{json.dumps(ir.model_dump_archify(), indent=2)}\n\n"
            f"Validator diagnostics ({len(diagnostics)} issue(s)):\n"
            f"{json.dumps(diagnostics, indent=2)}"
        )
        try:
            patched = backend.complete_json(
                _REPAIR_SYSTEM_PROMPT, user_prompt, schema, tool_name=_REPAIR_TOOL_NAME
            )
            ir = ArchitectureIR.model_validate(patched)
        except Exception as exc:
            # Layout repair is opportunistic. A provider quota/outage or an
            # invalid structured response must leave render() free to use
            # its deterministic, no-LLM fallback instead of crashing build.
            raise RepairUnavailableError("optional LLM layout repair is unavailable") from exc

    raise AssertionError("unreachable - loop always returns")  # pragma: no cover
