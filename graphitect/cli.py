"""graphitect CLI - argparse-based, no framework dependency, matching
graphify's own convention (plain sys.argv dispatch, see graphify/cli.py).

`build` is the product command. The older Ground -> Synthesize -> Deliver
subcommands remain available for debugging and advanced workflows.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from importlib import resources as importlib_resources
from pathlib import Path

from .deliver import archify_adapter, doc_compiler
from .ground import describe_source, graphify_source
from .models import AgentNarrative, Confidence, GroundedUnderstanding
from .synthesize import engine
from .synthesize.llm_backend import resolve_backend
from .synthesize.questions import apply_answer, read_answers, resolve_questions
from .synthesize.rubric import mine_rationale

_USAGE = """\
graphitect - grounded, sourced design docs for a system you already built

Usage:
  graphitect build <path> [--title TITLE] [--no-diagram]
                    [--ollama-local | --ollama-key KEY] [--anthropic-key KEY] [--gemini-key KEY]
                    [-o report.html]
  graphitect ground <path> [--mode graphify|describe] [--description TEXT] [-o out.json]
  graphitect synthesize <ground.json|understanding.json> [--repo PATH] [--diagram-kind KIND]
                        [--ollama-local | --ollama-key KEY] [--anthropic-key KEY] [--gemini-key KEY]
                        [--answers answers.json] [--non-interactive] [-o out.json]
  graphitect deliver <understanding.json> --title TITLE [-o out]
  graphitect agent prepare <path> [-o .graphitect-agent]
  graphitect agent compile <workspace> [--repo PATH] [--title TITLE] [--answers answers.json]
                           [--no-diagram] [-o report.html]
  graphitect skill export [--host codex|claude-code] [--global] [--output SKILL_DIRECTORY] [--force]

  graphitect --help
"""


def _skill_bundle() -> dict[str, str]:
    """Read the portable Agent Skills bundle from installed package data."""
    root = importlib_resources.files("graphitect").joinpath("skill")
    return {
        "SKILL.md": root.joinpath("SKILL.md").read_text(encoding="utf-8"),
        "agents/openai.yaml": root.joinpath("agents", "openai.yaml").read_text(encoding="utf-8"),
    }


def _cmd_skill_export(args: argparse.Namespace) -> int:
    """Export the same portable skill to a host's discovery location."""
    is_global = getattr(args, "global_scope", False)
    if args.output:
        destination = Path(args.output)
    elif args.host == "codex":
        destination = (Path.home() / ".agents" / "skills" / "graphitect") if is_global else (Path.cwd() / ".agents" / "skills" / "graphitect")
    elif args.host == "claude-code":
        destination = (Path.home() / ".claude" / "skills" / "graphitect") if is_global else (Path.cwd() / ".claude" / "skills" / "graphitect")
    else:
        print("error: --output is required when no host is selected", file=sys.stderr)
        return 2

    if destination.exists() and not args.force:
        print(
            f"error: skill destination already exists: {destination} (use --force to replace it)",
            file=sys.stderr,
        )
        return 2

    destination.mkdir(parents=True, exist_ok=True)
    for relative_path, content in _skill_bundle().items():
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    print(f"Exported Graphitect skill to {destination}")
    return 0


def _backend_args(args: argparse.Namespace):
    return resolve_backend(
        anthropic_key=getattr(args, "anthropic_key", None) or os.environ.get("ANTHROPIC_API_KEY"),
        gemini_key=getattr(args, "gemini_key", None)
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY"),
        ollama_key=getattr(args, "ollama_key", None) or os.environ.get("OLLAMA_API_KEY"),
        ollama_base_url=getattr(args, "ollama_base_url", None),
        ollama_model=getattr(args, "ollama_model", None),
        ollama_local=getattr(args, "ollama_local", False),
    )


def _render_archify_views(
    understanding: GroundedUnderstanding,
    title: str,
    overview_path: Path,
    story_path: Path,
    sequence_path: Path,
    full_path: Path,
    *,
    archify_bin: str | None,
    auto_install: bool,
    repair_backend,
    max_repair_iterations: int,
) -> tuple[
    str,
    str | None,
    str | None,
    str | None,
    Exception | None,
    Exception | None,
    Exception | None,
]:
    """Render the reliable overview plus optional Sequence, Workflow, and Full modes.

    Section 1 remains available if an enhanced mode cannot be validated. The
    caller receives the exceptions so it can disclose missing modes instead
    of silently claiming a Story or Full rollup exists.
    """
    common = {
        "archify_bin": archify_bin,
        "auto_install": auto_install,
        "repair_backend": repair_backend,
        "max_repair_iterations": max_repair_iterations,
    }
    archify_adapter.render(understanding, title, overview_path, view="overview", **common)
    overview_html = overview_path.read_text(encoding="utf-8")
    story_html = None
    story_error = None
    try:
        archify_adapter.render_workflow_story(
            understanding,
            title,
            story_path,
            archify_bin=archify_bin,
            auto_install=auto_install,
        )
        story_html = story_path.read_text(encoding="utf-8")
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as exc:
        story_error = exc
    sequence_html = None
    sequence_error = None
    try:
        archify_adapter.render_sequence_trace(
            understanding,
            title,
            sequence_path,
            archify_bin=archify_bin,
            auto_install=auto_install,
        )
        sequence_html = sequence_path.read_text(encoding="utf-8")
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as exc:
        sequence_error = exc
    try:
        archify_adapter.render(understanding, title, full_path, view="full", **common)
        return (
            overview_html,
            story_html,
            sequence_html,
            full_path.read_text(encoding="utf-8"),
            story_error,
            sequence_error,
            None,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        return overview_html, story_html, sequence_html, None, story_error, sequence_error, exc


def _cmd_build(args: argparse.Namespace) -> int:
    """One repository in, one self-contained HTML report out."""
    repo_path = Path(args.path).resolve()
    if not repo_path.is_dir():
        print(f"error: repository path is not a directory: {repo_path}", file=sys.stderr)
        return 2

    output_path = Path(args.output) if args.output else Path.cwd() / f"{repo_path.name}-graphitect.html"
    if output_path.suffix.lower() != ".html":
        output_path = output_path.with_suffix(".html")
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    title = args.title or repo_path.name

    build_temp = tempfile.TemporaryDirectory(prefix="graphitect-build-")
    try:
        temp_root = Path(build_temp.name)
        try:
            grounding = {
                "mode": "graphify",
                **graphify_source.ground(repo_path, output_dir=temp_root / "graphify-out"),
            }
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            print(f"error: bundled Graphify analysis failed: {exc}", file=sys.stderr)
            return 1

        try:
            backend = _backend_args(args)
        except RuntimeError:
            backend = None
        except ImportError as exc:
            print(f"error: bundled LLM provider support is unavailable: {exc}", file=sys.stderr)
            return 1

        if backend is None:
            understanding = engine.synthesize_diagram_only(grounding, diagram_kind="architecture")
            explanation_available = False
        else:
            understanding = engine.synthesize(
                grounding, repo_path, backend, diagram_kind="architecture"
            )
            explanation_available = True

        diagram_html = None
        story_diagram_html = None
        sequence_diagram_html = None
        full_diagram_html = None
        story_chapter_count = None
        story_hover_details = None
        sequence_message_count = None
        overview_connection_count = None
        full_connection_count = None
        if not args.no_diagram:
            overview_path = temp_root / "architecture-overview.html"
            story_path = temp_root / "architecture-story.html"
            sequence_path = temp_root / "architecture-sequence.html"
            full_path = temp_root / "architecture-full.html"
            try:
                (
                    diagram_html,
                    story_diagram_html,
                    sequence_diagram_html,
                    full_diagram_html,
                    story_error,
                    sequence_error,
                    full_error,
                ) = _render_archify_views(
                    understanding,
                    title,
                    overview_path,
                    story_path,
                    sequence_path,
                    full_path,
                    archify_bin=args.archify_bin,
                    auto_install=False,
                    repair_backend=None if args.no_repair else backend,
                    max_repair_iterations=args.max_repair_iterations,
                )
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                print(f"error: mandatory diagram generation failed: {exc}", file=sys.stderr)
                return 1
            if story_error is not None:
                print(
                    f"warning: workflow story could not be rendered; kept the overview ({story_error})",
                    file=sys.stderr,
                )
            elif story_diagram_html is not None:
                story_chapter_count = len(
                    archify_adapter.to_workflow_spec(understanding, title)["meta"]["views"]
                )
                story_hover_details = archify_adapter.workflow_hover_details(understanding)
            if sequence_error is not None:
                print(
                    f"note: sequence trace was not shown; it needs two consecutive direct Graphify calls/invocations ({sequence_error})",
                    file=sys.stderr,
                )
            elif sequence_diagram_html is not None:
                sequence_message_count = len(
                    archify_adapter.to_sequence_spec(understanding, title)["messages"]
                )
            if full_error is not None:
                print(
                    f"warning: full architecture rollup could not be rendered; kept the available modes ({full_error})",
                    file=sys.stderr,
                )
            else:
                overview_connection_count = len(
                    archify_adapter.to_architecture_ir(understanding, title, view="overview").connections
                )
                full_connection_count = len(
                    archify_adapter.to_architecture_ir(understanding, title, view="full").connections
                )

        node_id_remap = archify_adapter.node_id_remap(understanding) if diagram_html else None
        report = doc_compiler.to_html(
            understanding,
            title,
            diagram_svg=diagram_html,
            story_diagram_svg=story_diagram_html,
            story_hover_details=story_hover_details,
            sequence_diagram_svg=sequence_diagram_html,
            full_diagram_svg=full_diagram_html,
            story_chapter_count=story_chapter_count,
            sequence_message_count=sequence_message_count,
            overview_connection_count=overview_connection_count,
            full_connection_count=full_connection_count,
            node_id_remap=node_id_remap,
            explanation_available=explanation_available,
            diagram_opted_out=args.no_diagram,
        )
        staged_path = output_path.with_name(f".{output_path.name}.tmp")
        staged_path.write_text(report, encoding="utf-8")
        staged_path.replace(output_path)
        print(f"Wrote self-contained Graphitect report to {output_path}")
        return 0
    finally:
        build_temp.cleanup()


def _cmd_ground(args: argparse.Namespace) -> int:
    repo_path = Path(args.path)
    if args.mode == "describe":
        if not args.description:
            print("error: --description is required for --mode describe", file=sys.stderr)
            return 2
        claims = describe_source.ground(args.description)
        payload = {"mode": "describe", "claims": [c.model_dump() for c in claims]}
    else:
        graphed = graphify_source.ground(repo_path)
        payload = {"mode": "graphify", **graphed}

    out = Path(args.output) if args.output else repo_path / "graphitect-ground.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote grounding data to {out}")
    return 0


def _cmd_agent_prepare(args: argparse.Namespace) -> int:
    """Prepare deterministic evidence for a Codex/Claude-style host model.

    This command deliberately does not resolve an LLM backend. The installed
    agent reads the source packet and writes ``narrative.json`` with its own
    model; ``agent compile`` later validates and renders that work.
    """
    repo_path = Path(args.path).resolve()
    if not repo_path.is_dir():
        print(f"error: repository path is not a directory: {repo_path}", file=sys.stderr)
        return 2

    workspace = Path(args.output).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        grounding = {
            "mode": "graphify",
            **graphify_source.ground(repo_path, output_dir=workspace / "graphify-out"),
        }
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"error: bundled Graphify analysis failed: {exc}", file=sys.stderr)
        return 1

    files = {
        "grounding": "grounding.json",
        "context": "agent-context.md",
        "narrative": "narrative.json",
        "narrative_schema": "narrative.schema.json",
    }
    (workspace / files["grounding"]).write_text(json.dumps(grounding, indent=2), encoding="utf-8")
    (workspace / files["context"]).write_text(engine.build_context(grounding, repo_path), encoding="utf-8")
    (workspace / files["narrative"]).write_text(
        AgentNarrative().model_dump_json(indent=2), encoding="utf-8"
    )
    (workspace / files["narrative_schema"]).write_text(
        json.dumps(AgentNarrative.model_json_schema(), indent=2), encoding="utf-8"
    )
    (workspace / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "repo_path": str(repo_path), "files": files}, indent=2),
        encoding="utf-8",
    )
    print(f"Prepared host-model workspace at {workspace}")
    return 0


def _load_agent_workspace(workspace: Path, repo_override: str | None) -> tuple[Path, dict, AgentNarrative]:
    """Load a workspace written by ``agent prepare`` without trusting paths inside it."""
    try:
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 1:
            raise ValueError("unsupported manifest schema")
        files = manifest["files"]
        if files != {
            "grounding": "grounding.json",
            "context": "agent-context.md",
            "narrative": "narrative.json",
            "narrative_schema": "narrative.schema.json",
        }:
            raise ValueError("workspace manifest has unexpected artifact paths")
        grounding = json.loads((workspace / files["grounding"]).read_text(encoding="utf-8"))
        narrative = AgentNarrative.model_validate_json(
            (workspace / files["narrative"]).read_text(encoding="utf-8")
        )
        repo_path = Path(repo_override or manifest["repo_path"]).resolve()
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid agent workspace: {exc}") from exc

    if not repo_path.is_dir():
        raise ValueError(f"repository path is not a directory: {repo_path}")
    if grounding.get("mode") != "graphify":
        raise ValueError("agent workspace must contain Graphify grounding")
    return repo_path, grounding, narrative


def _downgrade_unsupported_agent_claims(understanding: GroundedUnderstanding) -> GroundedUnderstanding:
    """The host has no author-answer channel until ``--answers`` is applied.

    A host narrative therefore cannot claim a user said something, and every
    other confirmed assertion needs at least one citation for the verifier to
    check. Answers added by the shared question flow below remain valid user
    evidence because they are appended after this gate.
    """
    def downgrade(claim):
        if claim.confidence != Confidence.CONFIRMED:
            return claim
        if claim.cites and not any(evidence.source == "user" for evidence in claim.cites):
            return claim
        return claim.model_copy(
            update={
                "confidence": Confidence.INFERRED,
                "text": f"{claim.text} (citation could not be independently verified)",
            }
        )

    for section in understanding.doc:
        section.claims = [downgrade(claim) for claim in section.claims]
        for tradeoff in section.tradeoffs:
            tradeoff.decision = downgrade(tradeoff.decision)
            tradeoff.pros = [downgrade(claim) for claim in tradeoff.pros]
            tradeoff.cons = [downgrade(claim) for claim in tradeoff.cons]
    return understanding


def _cmd_agent_compile(args: argparse.Namespace) -> int:
    """Verify a host-authored narrative and render one self-contained report."""
    workspace = Path(args.workspace).resolve()
    try:
        repo_path, grounding, narrative = _load_agent_workspace(workspace, args.repo)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    real_node_ids = {node["id"] for node in grounding.get("nodes", []) if "id" in node}
    for section in narrative.doc:
        section.related_node_ids = [node_id for node_id in section.related_node_ids if node_id in real_node_ids]

    understanding = _downgrade_unsupported_agent_claims(
        GroundedUnderstanding(
            diagram_kind="architecture",
            nodes=grounding.get("nodes", []),
            edges=grounding.get("edges", []),
            doc=narrative.doc,
            community_labels=grounding.get("community_labels", {}),
        )
    )
    understanding = engine.verify_understanding(understanding, repo_path)
    try:
        understanding = _resolve_pending_questions(
            understanding,
            workspace,
            argparse.Namespace(answers=args.answers, non_interactive=True),
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: invalid --answers file: {exc}", file=sys.stderr)
        return 2

    (workspace / "understanding.json").write_text(
        understanding.model_dump_json(indent=2), encoding="utf-8"
    )
    if not understanding.pending_questions:
        (workspace / "graphitect-questions.json").write_text("[]\n", encoding="utf-8")

    output_path = Path(args.output) if args.output else workspace / f"{repo_path.name}-graphitect.html"
    if output_path.suffix.lower() != ".html":
        output_path = output_path.with_suffix(".html")
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    title = args.title or repo_path.name

    diagram_html = None
    story_diagram_html = None
    sequence_diagram_html = None
    full_diagram_html = None
    story_chapter_count = None
    story_hover_details = None
    sequence_message_count = None
    overview_connection_count = None
    full_connection_count = None
    render_temp = tempfile.TemporaryDirectory(prefix="graphitect-agent-")
    try:
        if not args.no_diagram:
            temp_root = Path(render_temp.name)
            try:
                (
                    diagram_html,
                    story_diagram_html,
                    sequence_diagram_html,
                    full_diagram_html,
                    story_error,
                    sequence_error,
                    full_error,
                ) = _render_archify_views(
                    understanding,
                    title,
                    temp_root / "architecture-overview.html",
                    temp_root / "architecture-story.html",
                    temp_root / "architecture-sequence.html",
                    temp_root / "architecture-full.html",
                    archify_bin=args.archify_bin,
                    auto_install=not args.no_auto_install,
                    repair_backend=None,
                    max_repair_iterations=0,
                )
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                print(f"error: mandatory diagram generation failed: {exc}", file=sys.stderr)
                return 1
            if story_error is not None:
                print(f"warning: workflow story could not be rendered ({story_error})", file=sys.stderr)
            elif story_diagram_html is not None:
                story_chapter_count = len(
                    archify_adapter.to_workflow_spec(understanding, title)["meta"]["views"]
                )
                story_hover_details = archify_adapter.workflow_hover_details(understanding)
            if sequence_error is not None:
                print(f"note: sequence trace was not shown ({sequence_error})", file=sys.stderr)
            elif sequence_diagram_html is not None:
                sequence_message_count = len(
                    archify_adapter.to_sequence_spec(understanding, title)["messages"]
                )
            if full_error is None:
                overview_connection_count = len(
                    archify_adapter.to_architecture_ir(understanding, title, view="overview").connections
                )
                full_connection_count = len(
                    archify_adapter.to_architecture_ir(understanding, title, view="full").connections
                )
            else:
                print(f"warning: full architecture rollup could not be rendered ({full_error})", file=sys.stderr)

        report = doc_compiler.to_html(
            understanding,
            title,
            diagram_svg=diagram_html,
            story_diagram_svg=story_diagram_html,
            story_hover_details=story_hover_details,
            sequence_diagram_svg=sequence_diagram_html,
            full_diagram_svg=full_diagram_html,
            story_chapter_count=story_chapter_count,
            sequence_message_count=sequence_message_count,
            overview_connection_count=overview_connection_count,
            full_connection_count=full_connection_count,
            node_id_remap=archify_adapter.node_id_remap(understanding) if diagram_html else None,
            explanation_available=True,
            diagram_opted_out=args.no_diagram,
        )
        staged_path = output_path.with_name(f".{output_path.name}.tmp")
        staged_path.write_text(report, encoding="utf-8")
        staged_path.replace(output_path)
    finally:
        render_temp.cleanup()

    print(f"Wrote host-model Graphitect report to {output_path}")
    return 0


def _resolve_pending_questions(
    understanding: GroundedUnderstanding, pending_dir: Path, args: argparse.Namespace
) -> GroundedUnderstanding:
    """Shared by both synthesize paths below: fold in --answers if given,
    otherwise resolve interactively or write graphitect-questions.json.

    pending_dir is where the *output* understanding.json is going, not
    wherever --repo happens to point - --repo is only ever a read-only
    reference for mining rationale from someone else's source tree, and
    defaulting the questions file into it silently wrote into a live
    project directory that had nothing to do with this output (confirmed
    live: a stray graphitect-questions.json landed inside FundersAI, only
    invisible to git by luck of an unrelated broad *.json ignore rule).
    """
    claims_by_id: dict[str, object] = {}
    for section in understanding.doc:
        for i, claim in enumerate(section.claims):
            claims_by_id[f"{section.heading}::{i}"] = claim

    pending = understanding.pending_questions

    if args.answers:
        answers = read_answers(Path(args.answers))
        for claim_id, answer_text in answers.items():
            if claim_id in claims_by_id:
                claims_by_id[claim_id] = apply_answer(claims_by_id[claim_id], answer_text)
        pending = [q for q in pending if q.claim_id not in answers]
    elif pending:
        pending_path = pending_dir / "graphitect-questions.json"
        claims_by_id, pending = resolve_questions(
            pending, claims_by_id, pending_path, non_interactive=args.non_interactive
        )
        if pending:
            print(f"{len(pending)} question(s) written to {pending_path}")

    for section in understanding.doc:
        for i in range(len(section.claims)):
            claim_id = f"{section.heading}::{i}"
            section.claims[i] = claims_by_id[claim_id]
    understanding.pending_questions = pending
    return understanding


def _cmd_synthesize(args: argparse.Namespace) -> int:
    """Two input shapes, detected by whether `diagram_kind` is present:

    - Raw grounding data (from `graphitect ground`, has a "mode" key): runs
      the LLM draft step (graphitect.synthesize.engine) to produce Claims/
      DesignDocSections for the first time, then verifies and queues
      questions - needs a configured standalone backend. Installed agent
      hosts instead use ``graphitect agent prepare`` / ``agent compile``.
    - An already-synthesized GroundedUnderstanding: re-runs only the
      deterministic rubric mining pass (no LLM call) on whatever's still
      `inferred` - useful after new evidence appears (a README update) or
      for hand-authored test inputs.
    """
    repo_path = Path(args.repo) if args.repo else Path.cwd()
    # encoding="utf-8" is required, not cosmetic: Path.read_text() without it
    # uses the platform default (cp1252 on Windows), which crashes on any
    # non-ASCII byte - confirmed live against FundersAI's real 7,885-node
    # graph, whose labels/community names include real-world unicode content
    # a small hand-authored test file would never happen to contain.
    raw = json.loads(Path(args.understanding).read_text(encoding="utf-8"))

    if "diagram_kind" in raw:
        understanding = GroundedUnderstanding.model_validate(raw)
        for section in understanding.doc:
            for i, claim in enumerate(section.claims):
                if claim.confidence != Confidence.CONFIRMED:
                    evidence = mine_rationale(repo_path, claim.text, claim.text.split())
                    if evidence:
                        claim = claim.model_copy(
                            update={"confidence": Confidence.CONFIRMED, "cites": [*claim.cites, evidence]}
                        )
                section.claims[i] = claim
        understanding = engine.verify_understanding(understanding, repo_path)
    else:
        try:
            backend = resolve_backend(
                anthropic_key=args.anthropic_key or os.environ.get("ANTHROPIC_API_KEY"),
                gemini_key=args.gemini_key
                or os.environ.get("GEMINI_API_KEY")
                or os.environ.get("GOOGLE_API_KEY"),
                ollama_key=args.ollama_key or os.environ.get("OLLAMA_API_KEY"),
                ollama_base_url=args.ollama_base_url,
                ollama_model=args.ollama_model,
                ollama_local=args.ollama_local,
            )
        except ImportError as exc:
            # A key WAS given but the matching optional dependency isn't
            # installed (e.g. --anthropic-key without
            # the combined Graphitect install) - a real, user-fixable
            # misconfiguration, not "no key at all". Keep failing loudly
            # rather than silently falling back, since the user clearly
            # intended a specific backend to be used here.
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except RuntimeError as exc:
            # No key configured at all. Graphify-mode grounding still has
            # real, deterministic diagram data with zero LLM involvement
            # (nodes/edges/community_labels) - only the narrative `doc` ever
            # needed a model, so hand back diagram-only data instead of
            # failing outright and producing nothing (Naman, 12 Sep 2026:
            # "the diagram is a must"). `describe` mode has no structural
            # data to fall back on - a free-text description genuinely needs
            # the LLM to become anything at all, so it keeps failing loudly.
            if raw.get("mode") != "graphify" or not raw.get("nodes"):
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(
                "warning: no LLM backend available - producing diagram data only, no "
                "design doc rationale (set ANTHROPIC_API_KEY/GEMINI_API_KEY/OLLAMA_API_KEY, "
                "or use `graphitect agent prepare` from an installed Agent Skill)",
                file=sys.stderr,
            )
            understanding = engine.synthesize_diagram_only(raw, diagram_kind=args.diagram_kind)
        else:
            understanding = engine.synthesize(raw, repo_path, backend, diagram_kind=args.diagram_kind)

    out = Path(args.output) if args.output else Path(args.understanding)
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        understanding = _resolve_pending_questions(understanding, out.parent, args)
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: invalid --answers file: {exc}", file=sys.stderr)
        return 2

    out.write_text(understanding.model_dump_json(indent=2), encoding="utf-8")
    print(f"Wrote synthesized understanding to {out}")
    return 0


def _resolve_repair_backend(args: argparse.Namespace):
    """Best-effort: unlike synthesize's resolve_backend() call, a missing or
    unusable LLM backend here is not an error - layout repair is a bonus on
    top of the deterministic heuristic (plan.md's layout-quality note), not
    something `deliver` should ever require. Any key already exported for
    `synthesize` (env vars) is picked up automatically for free.
    """
    try:
        return resolve_backend(
            anthropic_key=args.anthropic_key or os.environ.get("ANTHROPIC_API_KEY"),
            gemini_key=args.gemini_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY"),
            ollama_key=args.ollama_key or os.environ.get("OLLAMA_API_KEY"),
            ollama_base_url=args.ollama_base_url,
            ollama_model=args.ollama_model,
            ollama_local=args.ollama_local,
        )
    except (RuntimeError, ImportError):
        return None


def _cmd_deliver(args: argparse.Namespace) -> int:
    understanding = GroundedUnderstanding.model_validate_json(
        Path(args.understanding).read_text(encoding="utf-8")
    )
    out_base = Path(args.output) if args.output else Path("graphitect-deliverable")
    out_base.parent.mkdir(parents=True, exist_ok=True)
    repair_backend = None if args.no_repair else _resolve_repair_backend(args)

    md_path = out_base.with_suffix(".md")
    md_path.write_text(doc_compiler.to_markdown(understanding, args.title), encoding="utf-8")
    print(f"Wrote design doc to {md_path}")

    diagram_svg = None
    story_diagram_svg = None
    sequence_diagram_svg = None
    full_diagram_svg = None
    story_chapter_count = None
    story_hover_details = None
    sequence_message_count = None
    overview_connection_count = None
    full_connection_count = None
    if understanding.diagram_kind == "architecture" and understanding.nodes:
        # NOTE: out_base.with_name(...).with_suffix(".html") looked right but
        # isn't - .with_suffix() replaces the *last* suffix rather than
        # appending, so with_name(name + ".archify").with_suffix(".html")
        # silently collapses back to the exact same path as html_path below
        # (confirmed live: both evaluated to "live-test6.html"). It "worked"
        # only because this reads the file before html_path overwrites it -
        # a fragile coincidence, not a real distinct intermediate file.
        overview_archify_html = out_base.parent / f"{out_base.name}.overview.archify.html"
        story_archify_html = out_base.parent / f"{out_base.name}.story.archify.html"
        sequence_archify_html = out_base.parent / f"{out_base.name}.sequence.archify.html"
        full_archify_html = out_base.parent / f"{out_base.name}.full.archify.html"
        try:
            (
                diagram_svg,
                story_diagram_svg,
                sequence_diagram_svg,
                full_diagram_svg,
                story_error,
                sequence_error,
                full_error,
            ) = _render_archify_views(
                understanding,
                args.title,
                overview_archify_html,
                story_archify_html,
                sequence_archify_html,
                full_archify_html,
                archify_bin=args.archify_bin,
                auto_install=not args.no_auto_install,
                repair_backend=repair_backend,
                max_repair_iterations=args.max_repair_iterations,
            )
            print(f"Rendered overview diagram via archify ({overview_archify_html})")
            if story_error is None:
                print(f"Rendered workflow story via archify ({story_archify_html})")
                story_chapter_count = len(
                    archify_adapter.to_workflow_spec(understanding, args.title)["meta"]["views"]
                )
                story_hover_details = archify_adapter.workflow_hover_details(understanding)
            else:
                print(
                    f"warning: workflow story could not be rendered; kept the overview ({story_error})",
                    file=sys.stderr,
                )
            if sequence_error is None:
                print(f"Rendered evidence-gated sequence trace via archify ({sequence_archify_html})")
                sequence_message_count = len(
                    archify_adapter.to_sequence_spec(understanding, args.title)["messages"]
                )
            else:
                print(
                    f"note: sequence trace was not shown; it needs two consecutive direct Graphify calls/invocations ({sequence_error})",
                    file=sys.stderr,
                )
            if full_error is None:
                print(f"Rendered full architecture rollup via archify ({full_archify_html})")
                overview_connection_count = len(
                    archify_adapter.to_architecture_ir(understanding, args.title, view="overview").connections
                )
                full_connection_count = len(
                    archify_adapter.to_architecture_ir(understanding, args.title, view="full").connections
                )
            else:
                print(
                    f"warning: full architecture rollup could not be rendered; kept the available modes ({full_error})",
                    file=sys.stderr,
                )
        except FileNotFoundError as exc:
            print(f"warning: skipped diagram render - {exc}", file=sys.stderr)
        except subprocess.CalledProcessError as exc:
            # archify's own layout validator rejected the generated IR (e.g.
            # edges crossing unrelated components on a dense real-world
            # graph) - graphitect's grid heuristic doesn't do real connection
            # routing yet (see plan.md's layout-quality note), so this is a
            # known, reachable case on large graphs, not a crash-worthy bug.
            # Still deliver the design doc without a diagram rather than
            # dying with a raw traceback and producing nothing at all. If an
            # LLM repair backend was available, it already got a chance
            # (archify_repair.repair_and_deliver) and still didn't converge
            # within --max-repair-iterations.
            attempted = "with LLM repair" if repair_backend is not None else "no repair backend available"
            print(
                f"warning: skipped diagram render - archify rejected the layout "
                f"({attempted}) ({exc})",
                file=sys.stderr,
            )

    # Only needed (and only meaningful) when a diagram actually got embedded -
    # doc_compiler.node_id_remap default (identity) is fine when there's no
    # diagram_svg to highlight into at all.
    node_id_remap = archify_adapter.node_id_remap(understanding) if diagram_svg else None

    html_path = out_base.with_suffix(".html")
    html_path.write_text(
        doc_compiler.to_html(
            understanding,
            args.title,
            diagram_svg=diagram_svg,
            story_diagram_svg=story_diagram_svg,
            story_hover_details=story_hover_details,
            sequence_diagram_svg=sequence_diagram_svg,
            full_diagram_svg=full_diagram_svg,
            story_chapter_count=story_chapter_count,
            sequence_message_count=sequence_message_count,
            overview_connection_count=overview_connection_count,
            full_connection_count=full_connection_count,
            node_id_remap=node_id_remap,
        ),
        encoding="utf-8",
    )
    print(f"Wrote design doc (HTML) to {html_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="graphitect", usage=_USAGE, add_help=True)
    sub = parser.add_subparsers(dest="command")

    p_build = sub.add_parser("build", help="analyze a repository and write one HTML report")
    p_build.add_argument("path")
    p_build.add_argument("--title")
    p_build.add_argument("--no-diagram", action="store_true")
    p_build.add_argument("--archify-bin", help=argparse.SUPPRESS)
    p_build.add_argument("--anthropic-key")
    p_build.add_argument("--gemini-key")
    p_build.add_argument("--ollama-key")
    p_build.add_argument("--ollama-base-url")
    p_build.add_argument("--ollama-model")
    p_build.add_argument("--ollama-local", action="store_true")
    p_build.add_argument("--no-repair", action="store_true")
    p_build.add_argument("--max-repair-iterations", type=int, default=3)
    p_build.add_argument("-o", "--output")
    p_build.set_defaults(func=_cmd_build)

    p_ground = sub.add_parser("ground")
    p_ground.add_argument("path")
    p_ground.add_argument("--mode", choices=["graphify", "describe"], default="graphify")
    p_ground.add_argument("--description")
    p_ground.add_argument("-o", "--output")
    p_ground.set_defaults(func=_cmd_ground)

    p_synth = sub.add_parser("synthesize")
    p_synth.add_argument("understanding")
    p_synth.add_argument("--repo")
    p_synth.add_argument("--diagram-kind", default="architecture")
    p_synth.add_argument("--anthropic-key")
    p_synth.add_argument("--gemini-key")
    p_synth.add_argument("--ollama-key", help="Ollama Cloud API key (free tier)")
    p_synth.add_argument("--ollama-base-url", help="override, e.g. for a self-hosted Ollama server")
    p_synth.add_argument("--ollama-model", help="default: llama3.1")
    p_synth.add_argument(
        "--ollama-local", action="store_true", help="use a local Ollama server, no key needed"
    )
    p_synth.add_argument("--answers")
    p_synth.add_argument("--non-interactive", action="store_true")
    p_synth.add_argument("-o", "--output")
    p_synth.set_defaults(func=_cmd_synthesize)

    p_deliver = sub.add_parser("deliver")
    p_deliver.add_argument("understanding")
    p_deliver.add_argument("--title", required=True)
    p_deliver.add_argument(
        "--archify-bin", help="e.g. 'node /path/to/archify/bin/archify.mjs' if not on PATH"
    )
    p_deliver.add_argument(
        "--no-auto-install",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p_deliver.add_argument("--anthropic-key")
    p_deliver.add_argument("--gemini-key")
    p_deliver.add_argument("--ollama-key", help="Ollama Cloud API key (free tier)")
    p_deliver.add_argument("--ollama-base-url", help="override, e.g. for a self-hosted Ollama server")
    p_deliver.add_argument("--ollama-model", help="default: llama3.1")
    p_deliver.add_argument(
        "--ollama-local", action="store_true", help="use a local Ollama server, no key needed"
    )
    p_deliver.add_argument(
        "--no-repair",
        action="store_true",
        help="don't attempt LLM-driven layout repair even if a backend key is available",
    )
    p_deliver.add_argument(
        "--max-repair-iterations",
        type=int,
        default=3,
        help="max archify-deliver attempts when a repair backend is available (default: 3)",
    )
    p_deliver.add_argument("-o", "--output")
    p_deliver.set_defaults(func=_cmd_deliver)

    p_agent = sub.add_parser(
        "agent", help="prepare or compile a no-key handoff for an installed agent host"
    )
    p_agent_sub = p_agent.add_subparsers(dest="agent_command", required=True)
    p_agent_prepare = p_agent_sub.add_parser(
        "prepare", help="write grounded evidence for the host model; never calls an LLM"
    )
    p_agent_prepare.add_argument("path")
    p_agent_prepare.add_argument("-o", "--output", default=".graphitect-agent")
    p_agent_prepare.set_defaults(func=_cmd_agent_prepare)

    p_agent_compile = p_agent_sub.add_parser(
        "compile", help="verify a host-written narrative and write one self-contained report"
    )
    p_agent_compile.add_argument("workspace")
    p_agent_compile.add_argument("--repo", help="override the repository path recorded during prepare")
    p_agent_compile.add_argument("--title")
    p_agent_compile.add_argument("--answers")
    p_agent_compile.add_argument("--no-diagram", action="store_true")
    p_agent_compile.add_argument("--archify-bin", help=argparse.SUPPRESS)
    p_agent_compile.add_argument("--no-auto-install", action="store_true", help=argparse.SUPPRESS)
    p_agent_compile.add_argument("-o", "--output")
    p_agent_compile.set_defaults(func=_cmd_agent_compile)

    p_skill = sub.add_parser("skill", help="export the portable Graphitect Agent Skill")
    p_skill_sub = p_skill.add_subparsers(dest="skill_command", required=True)
    p_skill_export = p_skill_sub.add_parser("export", help="write a Graphitect skill bundle")
    p_skill_export.add_argument("--host", choices=["codex", "claude-code"])
    p_skill_export.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="install globally into the user home directory (~/.claude or ~/.agents) instead of the current project",
    )
    p_skill_export.add_argument(
        "--output",
        help="destination skill directory for another Agent Skills-compatible host",
    )
    p_skill_export.add_argument(
        "--force", action="store_true", help="replace files in an existing skill directory"
    )
    p_skill_export.set_defaults(func=_cmd_skill_export)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        print(_USAGE)
        sys.exit(0 if argv and "--help" in argv else 1)
    sys.exit(args.func(args))
