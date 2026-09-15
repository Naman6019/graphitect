import json
from pathlib import Path
from unittest.mock import patch

import pytest

from graphitect.deliver.archify_ir import ArchitectureIR, Component, Connection, Meta
from graphitect.deliver.archify_repair import (
    RepairUnavailableError,
    repair_and_deliver,
    run_deliver_json,
)


class FakeRepairBackend:
    """A canned backend for testing the repair loop without a live API call
    - mirrors test_engine.py's FakeBackend pattern, but implements
    complete_json (the general structured-output method the repair loop
    uses) rather than draft (Synthesize's).
    """

    def __init__(self, patches: list[dict]):
        self._patches = list(patches)
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system_prompt: str, user_prompt: str, schema: dict, *, tool_name: str) -> dict:
        self.calls.append((system_prompt, user_prompt))
        return self._patches.pop(0)


class UnavailableRepairBackend:
    def complete_json(self, *args, **kwargs) -> dict:
        raise RuntimeError("provider quota exceeded")


def _make_ir() -> ArchitectureIR:
    return ArchitectureIR(
        meta=Meta(title="Test"),
        components=[
            Component(id="api", type="backend", label="API", row=0, col=0),
            Component(id="db", type="database", label="Database", row=1, col=0),
        ],
        connections=[Connection(id="api-db", **{"from": "api"}, to="db", label="SQL")],
    )


def _fake_subprocess_run(reports: list[dict]):
    """Returns a stand-in for subprocess.run that yields each report in
    order via a fake CompletedProcess whose stdout is that report's JSON -
    matching how archify's real `--json` flag reports success/failure on
    stdout regardless of exit code (confirmed live, 12 Sep 2026).
    """
    remaining = list(reports)

    def _run(*args, **kwargs):
        report = remaining.pop(0)

        class _Result:
            stdout = json.dumps(report)
            stderr = ""
            returncode = 0 if report.get("ok") else 1

        return _Result()

    return _run


_DIAGNOSTIC = {
    "code": "layout/constraint",
    "severity": "error",
    "message": 'Label "SQL" overlaps component "api" - Suggested fix: labelAt [100, 154]',
    "subject": {"diagramType": "architecture"},
    "evidence": {},
    "supportedFixes": [],
}


def test_repair_returns_immediately_when_the_first_attempt_already_passes(tmp_path: Path):
    # No LLM call should ever happen when the deterministic layout already
    # validates - repair is a fallback for rejected layouts, not a tax on
    # every deliver call.
    backend = FakeRepairBackend(patches=[])
    with patch(
        "graphitect.deliver.archify_repair.subprocess.run",
        side_effect=_fake_subprocess_run([{"ok": True}]),
    ):
        report = repair_and_deliver(
            _make_ir(), ["archify"], tmp_path / "ir.json", tmp_path / "out.html", backend
        )
    assert report["ok"] is True
    assert backend.calls == []


def test_repair_patches_the_ir_and_converges_on_the_second_attempt(tmp_path: Path):
    # Regression scenario: the initial IR fails with a real archify
    # diagnostic (a label overlapping a component, with a concrete
    # "Suggested fix" - confirmed live against FundersAI's real graph), the
    # backend is asked to patch it, and the patched IR is what gets tried
    # next and succeeds.
    patched_ir = json.loads(_make_ir().model_dump_json())
    patched_ir["connections"][0]["labelAt"] = [100, 154]
    backend = FakeRepairBackend(patches=[patched_ir])

    reports = [
        {"ok": False, "diagnostics": [_DIAGNOSTIC]},
        {"ok": True},
    ]
    with patch(
        "graphitect.deliver.archify_repair.subprocess.run", side_effect=_fake_subprocess_run(reports)
    ):
        report = repair_and_deliver(
            _make_ir(), ["archify"], tmp_path / "ir.json", tmp_path / "out.html", backend
        )

    assert report["ok"] is True
    assert len(backend.calls) == 1
    _, user_prompt = backend.calls[0]
    assert "layout/constraint" in user_prompt
    assert "labelAt [100, 154]" in user_prompt


def test_repair_gives_up_after_max_iterations_without_a_final_wasted_patch_call(tmp_path: Path):
    # A layout that never converges must still return (the still-failing
    # report) rather than loop forever or crash - the caller (render())
    # falls back to a diagramless doc from there. The last attempt must not
    # trigger one more pointless patch call that will never be tried.
    backend = FakeRepairBackend(patches=[_make_ir().model_dump(), _make_ir().model_dump()])
    always_failing = [{"ok": False, "diagnostics": [_DIAGNOSTIC]} for _ in range(3)]
    with patch(
        "graphitect.deliver.archify_repair.subprocess.run",
        side_effect=_fake_subprocess_run(always_failing),
    ):
        report = repair_and_deliver(
            _make_ir(),
            ["archify"],
            tmp_path / "ir.json",
            tmp_path / "out.html",
            backend,
            max_iterations=3,
        )

    assert report["ok"] is False
    assert len(backend.calls) == 2  # patches after attempt 1 and 2, none after attempt 3


def test_repair_stops_immediately_if_a_failure_carries_no_diagnostics(tmp_path: Path):
    # A failure archify's --json didn't attach diagnostics to (unexpected,
    # but the code shouldn't hand the backend an empty issue list and hope
    # for the best).
    backend = FakeRepairBackend(patches=[])
    with patch(
        "graphitect.deliver.archify_repair.subprocess.run",
        side_effect=_fake_subprocess_run([{"ok": False, "diagnostics": []}]),
    ):
        report = repair_and_deliver(
            _make_ir(), ["archify"], tmp_path / "ir.json", tmp_path / "out.html", backend
        )
    assert report["ok"] is False
    assert backend.calls == []


def test_repair_wraps_a_provider_failure_so_render_can_fall_back(tmp_path: Path):
    with patch(
        "graphitect.deliver.archify_repair.subprocess.run",
        side_effect=_fake_subprocess_run([{"ok": False, "diagnostics": [_DIAGNOSTIC]}]),
    ), pytest.raises(RepairUnavailableError, match="optional LLM layout repair"):
        repair_and_deliver(
            _make_ir(),
            ["archify"],
            tmp_path / "ir.json",
            tmp_path / "out.html",
            UnavailableRepairBackend(),
        )


def test_repair_rejects_a_non_positive_max_iterations(tmp_path: Path):
    with pytest.raises(ValueError):
        repair_and_deliver(
            _make_ir(),
            ["archify"],
            tmp_path / "ir.json",
            tmp_path / "out.html",
            FakeRepairBackend(patches=[]),
            max_iterations=0,
        )


def test_deliver_json_decodes_archify_output_as_utf8(tmp_path: Path):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)

        class Result:
            stdout = '{"ok": true, "label": "\u96ea"}'
            stderr = ""
            returncode = 0

        return Result()

    with patch("graphitect.deliver.archify_repair.subprocess.run", side_effect=fake_run):
        report = run_deliver_json(["archify"], tmp_path / "ir.json", tmp_path / "out.html")

    assert report["label"] == "\u96ea"
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
