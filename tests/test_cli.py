import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from graphitect.cli import main
from graphitect.models import QuestionOption, RationaleQuestion
from graphitect.synthesize.questions import write_pending


@pytest.fixture
def anthropic_package_absent(monkeypatch):
    """Force `import anthropic` to fail with ModuleNotFoundError regardless
    of whether the package is actually installed in this environment - the
    ImportError code path must never depend on venv state, and must never
    let a real network call slip through with a bogus key (which happened
    once already: installing `anthropic` for an unrelated schema-check
    turned this test into a live 401 call to Anthropic's API).
    """
    monkeypatch.setitem(sys.modules, "anthropic", None)


def test_synthesize_raw_grounding_with_no_key_exits_cleanly(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    ground_file = tmp_path / "ground.json"
    ground_file.write_text('{"mode": "describe", "claims": []}', encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main(["synthesize", str(ground_file), "--repo", str(tmp_path)])

    assert exc_info.value.code == 2
    assert "No LLM backend available" in capsys.readouterr().err


def test_build_is_one_command_one_html_output_without_an_llm(tmp_path: Path):
    repo = tmp_path / "sample-project"
    repo.mkdir()
    output = tmp_path / "report.html"

    grounding = {
        "nodes": [{"id": "api", "label": "API"}],
        "edges": [],
        "communities": {},
        "community_labels": {},
    }

    rendered_views = []

    def fake_render(understanding, title, out_path, **kwargs):
        rendered_views.append(kwargs["view"])
        out_path.write_text(
            "<html><script>window.archifyViewer=true</script><svg id=\"api\"></svg></html>",
            encoding="utf-8",
        )
        return out_path

    with patch("graphitect.cli.graphify_source.ground", return_value=grounding), patch(
        "graphitect.cli._backend_args", side_effect=RuntimeError("no key")
    ), patch("graphitect.cli.archify_adapter.render", side_effect=fake_render):
        with pytest.raises(SystemExit) as exc_info:
            main(["build", str(repo), "-o", str(output)])

    assert exc_info.value.code == 0
    assert [path.name for path in tmp_path.iterdir() if path.is_file()] == ["report.html"]
    html = output.read_text(encoding="utf-8")
    assert "Interactive system diagram" in html
    assert "window.archifyViewer=true" in html
    assert "LLM required" in html
    assert rendered_views == ["overview", "full"]
    assert "Full architecture rollup" in html


def test_build_can_explicitly_opt_out_of_the_diagram(tmp_path: Path):
    repo = tmp_path / "sample-project"
    repo.mkdir()
    output = tmp_path / "report.html"

    grounding = {
        "nodes": [{"id": "api", "label": "API"}],
        "edges": [],
        "communities": {},
        "community_labels": {},
    }

    with patch("graphitect.cli.graphify_source.ground", return_value=grounding), patch(
        "graphitect.cli._backend_args", side_effect=RuntimeError("no key")
    ), patch("graphitect.cli.archify_adapter.render") as render:
        with pytest.raises(SystemExit) as exc_info:
            main(["build", str(repo), "--no-diagram", "-o", str(output)])

    assert exc_info.value.code == 0
    render.assert_not_called()
    assert "explicitly opted out" in output.read_text(encoding="utf-8")


def test_agent_prepare_writes_grounded_handoff_without_resolving_an_llm(tmp_path: Path):
    repo = tmp_path / "sample-project"
    repo.mkdir()
    (repo / "README.md").write_text("A sample project.\n", encoding="utf-8")
    workspace = tmp_path / "agent-workspace"
    grounding = {
        "nodes": [{"id": "api", "label": "API", "source_file": "README.md"}],
        "edges": [],
        "communities": {},
        "community_labels": {},
    }

    with patch("graphitect.cli.graphify_source.ground", return_value=grounding), patch(
        "graphitect.cli.resolve_backend"
    ) as resolve_backend, pytest.raises(SystemExit) as exc_info:
        main(["agent", "prepare", str(repo), "-o", str(workspace)])

    assert exc_info.value.code == 0
    resolve_backend.assert_not_called()
    assert json.loads((workspace / "grounding.json").read_text(encoding="utf-8"))["nodes"] == grounding[
        "nodes"
    ]
    assert json.loads((workspace / "narrative.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "doc": [],
    }
    assert "Structure graph: 1 nodes, 0 edges." in (workspace / "agent-context.md").read_text(
        encoding="utf-8"
    )


def test_agent_compile_uses_host_narrative_but_keeps_graphify_structure_and_verifies_it(tmp_path: Path):
    repo = tmp_path / "sample-project"
    repo.mkdir()
    (repo / "service.py").write_text(
        "# Persists workflow state in the repository store.\n", encoding="utf-8"
    )
    workspace = tmp_path / "agent-workspace"
    workspace.mkdir()
    (workspace / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repo_path": str(repo),
                "files": {
                    "grounding": "grounding.json",
                    "context": "agent-context.md",
                    "narrative": "narrative.json",
                    "narrative_schema": "narrative.schema.json",
                },
            }
        ),
        encoding="utf-8",
    )
    (workspace / "grounding.json").write_text(
        json.dumps(
            {
                "mode": "graphify",
                "nodes": [{"id": "api", "label": "API"}],
                "edges": [],
                "community_labels": {},
            }
        ),
        encoding="utf-8",
    )
    (workspace / "narrative.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "doc": [
                    {
                        "heading": "Technology choices & why",
                        "related_node_ids": ["api", "invented-node"],
                        "claims": [
                            {
                                "text": "Workflow state is persisted in the repository store",
                                "confidence": "confirmed",
                                "cites": [
                                    {
                                        "source": "code",
                                        "file": "service.py",
                                        "note": "Persists workflow state in the repository store.",
                                    }
                                ],
                            },
                            {
                                "text": "The store scales horizontally",
                                "confidence": "confirmed",
                                "cites": [],
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "report.html"

    with patch("graphitect.cli.resolve_backend") as resolve_backend, pytest.raises(SystemExit) as exc_info:
        main(["agent", "compile", str(workspace), "--no-diagram", "-o", str(output)])

    assert exc_info.value.code == 0
    resolve_backend.assert_not_called()
    understanding = json.loads((workspace / "understanding.json").read_text(encoding="utf-8"))
    assert understanding["nodes"] == [{"id": "api", "label": "API"}]
    assert understanding["doc"][0]["related_node_ids"] == ["api"]
    assert understanding["doc"][0]["claims"][0]["confidence"] == "confirmed"
    assert understanding["doc"][0]["claims"][1]["confidence"] == "inferred"
    assert output.is_file()
    assert "Project explanation" in output.read_text(encoding="utf-8")
    assert json.loads((workspace / "graphitect-questions.json").read_text(encoding="utf-8"))


def test_synthesize_raw_grounding_with_key_but_missing_package_exits_cleanly(
    tmp_path: Path, capsys, anthropic_package_absent
):
    # Regression test: resolve_backend's AnthropicBackend raises ImportError
    # (not RuntimeError) when a key is given but the `anthropic` package
    # isn't installed - this previously escaped as an uncaught traceback
    # instead of a clean, user-fixable error message. Must never fall
    # through to a real API call - see anthropic_package_absent's docstring
    # for why that's not hypothetical.
    ground_file = tmp_path / "ground.json"
    ground_file.write_text('{"mode": "describe", "claims": []}', encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "synthesize",
                str(ground_file),
                "--repo",
                str(tmp_path),
                "--anthropic-key",
                "fake-key-not-real",
            ]
        )

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "pip install --force-reinstall graphitect" in err


def test_synthesize_graphify_grounding_with_no_key_still_produces_a_diagram(
    tmp_path: Path, monkeypatch, capsys
):
    # Naman's requirement (12 Sep 2026): "the diagram is a must" - unlike
    # describe-mode (which genuinely needs an LLM to produce anything at
    # all), graphify-mode grounding already has real, deterministic
    # nodes/edges with zero LLM involvement, so having no key configured
    # must not block getting a diagram - only the narrative doc is skipped.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    ground_file = tmp_path / "ground.json"
    ground_file.write_text(
        json.dumps(
            {
                "mode": "graphify",
                "nodes": [{"id": "api", "label": "API"}, {"id": "db", "label": "Database"}],
                "edges": [{"from": "api", "to": "db"}],
            }
        ),
        encoding="utf-8",
    )
    out_file = tmp_path / "understanding.json"

    with pytest.raises(SystemExit) as exc_info:
        main(["synthesize", str(ground_file), "-o", str(out_file)])

    assert exc_info.value.code == 0
    assert "no LLM backend available" in capsys.readouterr().err
    result = json.loads(out_file.read_text(encoding="utf-8"))
    assert result["nodes"] == [{"id": "api", "label": "API"}, {"id": "db", "label": "Database"}]
    assert result["edges"] == [{"from": "api", "to": "db"}]
    assert len(result["doc"]) == 1  # the honest structure-only overview claim


def test_synthesize_graphify_grounding_with_no_nodes_and_no_key_still_fails(
    tmp_path: Path, monkeypatch, capsys
):
    # Edge case: "graphify" mode but genuinely no structural data (e.g. an
    # empty repo) - there's nothing to fall back to, so this must still
    # fail loudly rather than silently hand back an empty, diagramless doc.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    ground_file = tmp_path / "ground.json"
    ground_file.write_text(json.dumps({"mode": "graphify", "nodes": [], "edges": []}), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main(["synthesize", str(ground_file)])

    assert exc_info.value.code == 2
    assert "No LLM backend available" in capsys.readouterr().err


def test_synthesize_writes_pending_questions_next_to_output_not_into_repo(tmp_path: Path):
    # Regression test for a real live bug: pending questions defaulted to
    # writing into --repo (a read-only reference for mining rationale from
    # someone else's source tree), which silently landed a stray
    # graphitect-questions.json inside a live project (FundersAI) that had
    # nothing to do with this command's actual output - only invisible to
    # git by luck of an unrelated broad *.json ignore rule there. Questions
    # must land next to the *output* understanding.json instead.
    reference_repo = tmp_path / "someone_elses_repo"
    reference_repo.mkdir()
    output_dir = tmp_path / "my_output"

    understanding_file = tmp_path / "understanding.json"
    understanding_file.write_text(
        json.dumps(
            {
                "diagram_kind": "architecture",
                "nodes": [],
                "edges": [],
                "doc": [
                    {
                        "heading": "Technology choices & why",
                        "claims": [
                            {
                                "text": "Chose FastAPI",
                                "confidence": "inferred",
                                "cites": [],
                                "kind": "descriptive",
                            }
                        ],
                    }
                ],
                "pending_questions": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "synthesize",
                str(understanding_file),
                "--repo",
                str(reference_repo),
                "--non-interactive",
                "-o",
                str(output_dir / "understanding.json"),
            ]
        )

    assert exc_info.value.code == 0
    assert not (reference_repo / "graphitect-questions.json").exists()
    assert (output_dir / "graphitect-questions.json").exists()


def test_synthesize_accepts_the_generated_questions_file_as_answers(tmp_path: Path):
    question = RationaleQuestion(
        claim_id="Technology choices & why::0",
        question="Why FastAPI?",
        options=[QuestionOption(label="Async support", becomes_text="Chose FastAPI for async support")],
    )
    answers_path = tmp_path / "graphitect-questions.json"
    write_pending([question], answers_path)
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    answers[0]["answer"] = "Chose FastAPI for async support"
    answers_path.write_text(json.dumps(answers), encoding="utf-8")

    understanding_path = tmp_path / "understanding.json"
    understanding_path.write_text(
        json.dumps(
            {
                "diagram_kind": "architecture",
                "nodes": [],
                "edges": [],
                "doc": [
                    {
                        "heading": "Technology choices & why",
                        "claims": [{"text": "Chose FastAPI", "confidence": "inferred"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        main(["synthesize", str(understanding_path), "--repo", str(tmp_path), "--answers", str(answers_path)])

    assert exc_info.value.code == 0
    result = json.loads(understanding_path.read_text(encoding="utf-8"))
    assert result["doc"][0]["claims"][0]["confidence"] == "confirmed"
    assert result["pending_questions"] == []


def test_synthesize_reads_non_ascii_understanding_json_correctly(tmp_path: Path):
    # Regression test for a real live bug: Path.read_text() without an
    # explicit encoding uses the platform default (cp1252 on Windows), which
    # crashes on any non-ASCII byte. Confirmed live against FundersAI's real
    # 7,885-node graph - a small hand-authored test file would never happen
    # to contain the kind of real-world unicode content that triggered it,
    # so this test deliberately includes some.
    understanding_file = tmp_path / "understanding.json"
    understanding_file.write_text(
        json.dumps(
            {
                "diagram_kind": "architecture",
                "nodes": [],
                "edges": [],
                "doc": [
                    {
                        "heading": "Overview",
                        "claims": [
                            {
                                "text": "Uses emoji \U0001f680 and CJK 中文 in a real label",
                                "confidence": "confirmed",
                                "cites": [],
                                "kind": "descriptive",
                            }
                        ],
                    }
                ],
                "pending_questions": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        main(["synthesize", str(understanding_file), "--non-interactive"])

    assert exc_info.value.code == 0
    result = json.loads(understanding_file.read_text(encoding="utf-8"))
    assert "\U0001f680" in result["doc"][0]["claims"][0]["text"]


def test_deliver_archify_intermediate_paths_are_distinct_from_final_output(tmp_path: Path):
    # Regression test for a real live bug: the intermediate archify-rendered
    # file's path was computed as out_base.with_name(name + ".archify")
    # .with_suffix(".html"), which silently collapses to the exact same path
    # as the final html output (.with_suffix() replaces the *last* suffix,
    # it doesn't append one) - confirmed live, both evaluated to the same
    # "live-test6.html". It happened to "work" only because the code reads
    # the intermediate before overwriting it with the final doc; asserting
    # the paths differ here makes sure a reorder can't silently break it.
    understanding_file = tmp_path / "understanding.json"
    understanding_file.write_text(
        '{"diagram_kind": "architecture", "nodes": [{"id": "api", "label": "API"}, '
        '{"id": "service", "label": "Service"}, {"id": "db", "label": "Database"}], '
        '"edges": [{"source": "api", "target": "service", "relation": "calls"}, '
        '{"source": "service", "target": "db", "relation": "calls"}], "doc": []}',
        encoding="utf-8",
    )
    out_base = tmp_path / "out" / "deliverable"

    captured_paths = []

    def fake_render(understanding, title, out_path, **kwargs):
        captured_paths.append(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("<html><svg id=\"x\"></svg></html>", encoding="utf-8")
        return out_path

    def fake_workflow_render(understanding, title, out_path, **kwargs):
        captured_paths.append(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("<html><svg id=\"x\"></svg></html>", encoding="utf-8")
        return out_path

    def fake_sequence_render(understanding, title, out_path, **kwargs):
        captured_paths.append(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("<html><svg id=\"x\"></svg></html>", encoding="utf-8")
        return out_path

    with patch("graphitect.deliver.archify_adapter.render", side_effect=fake_render), patch(
        "graphitect.deliver.archify_adapter.render_workflow_story",
        side_effect=fake_workflow_render,
    ), patch(
        "graphitect.deliver.archify_adapter.render_sequence_trace",
        side_effect=fake_sequence_render,
    ), pytest.raises(SystemExit) as exc_info:
        main(["deliver", str(understanding_file), "--title", "Test", "-o", str(out_base)])

    assert exc_info.value.code == 0
    assert len(captured_paths) == 4
    overview_intermediate, story_intermediate, sequence_intermediate, full_intermediate = captured_paths
    final_html = out_base.with_suffix(".html")
    assert overview_intermediate != final_html
    assert story_intermediate != final_html
    assert sequence_intermediate != final_html
    assert full_intermediate != final_html
    assert overview_intermediate != full_intermediate
    assert story_intermediate != full_intermediate
    assert sequence_intermediate != full_intermediate
    assert overview_intermediate.exists()
    assert story_intermediate.exists()
    assert full_intermediate.exists()
    assert final_html.exists()


def test_deliver_still_writes_the_doc_when_archify_rejects_the_layout(tmp_path: Path, capsys):
    # Regression test for a real live bug: archify's own layout validator
    # rejecting a dense real-world graph (edges crossing unrelated
    # components - graphitect's grid heuristic doesn't do real connection
    # routing) raised an uncaught subprocess.CalledProcessError straight out
    # of _cmd_deliver, crashing the whole command with a traceback and
    # producing no output at all - confirmed live against FundersAI's real
    # graph. A rejected diagram must degrade the same way a missing archify
    # binary already does: warn on stderr, still deliver the design doc.
    understanding_file = tmp_path / "understanding.json"
    understanding_file.write_text(
        '{"diagram_kind": "architecture", "nodes": [{"id": "api", "label": "API"}], '
        '"edges": [], "doc": []}',
        encoding="utf-8",
    )
    out_base = tmp_path / "out" / "deliverable"

    def fake_render(understanding, title, out_path, **kwargs):
        raise subprocess.CalledProcessError(1, ["archify", "deliver"])

    with patch("graphitect.deliver.archify_adapter.render", side_effect=fake_render):
        with pytest.raises(SystemExit) as exc_info:
            main(["deliver", str(understanding_file), "--title", "Test", "-o", str(out_base)])

    assert exc_info.value.code == 0
    assert "archify rejected the layout" in capsys.readouterr().err
    assert out_base.with_suffix(".md").exists()
    assert out_base.with_suffix(".html").exists()


def test_deliver_passes_an_available_backend_through_for_layout_repair(tmp_path: Path, monkeypatch):
    # deliver should pick up a repair backend from the same env vars/flags
    # synthesize already uses (an OLLAMA_API_KEY exported for synthesize
    # shouldn't need re-specifying for deliver) and thread it into
    # archify_adapter.render() - opportunistically, never required.
    monkeypatch.setenv("OLLAMA_API_KEY", "fake-key-not-real")
    understanding_file = tmp_path / "understanding.json"
    understanding_file.write_text(
        '{"diagram_kind": "architecture", "nodes": [{"id": "api", "label": "API"}], '
        '"edges": [], "doc": []}',
        encoding="utf-8",
    )
    out_base = tmp_path / "out" / "deliverable"
    captured = {}

    def fake_render(understanding, title, out_path, **kwargs):
        captured["repair_backend"] = kwargs.get("repair_backend")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("<html><svg id=\"x\"></svg></html>", encoding="utf-8")
        return out_path

    with patch("graphitect.deliver.archify_adapter.render", side_effect=fake_render):
        with pytest.raises(SystemExit):
            main(["deliver", str(understanding_file), "--title", "Test", "-o", str(out_base)])

    from graphitect.synthesize.llm_backend import OllamaBackend

    assert isinstance(captured["repair_backend"], OllamaBackend)


def test_deliver_no_repair_flag_skips_backend_resolution_even_with_a_key_present(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OLLAMA_API_KEY", "fake-key-not-real")
    understanding_file = tmp_path / "understanding.json"
    understanding_file.write_text(
        '{"diagram_kind": "architecture", "nodes": [{"id": "api", "label": "API"}], '
        '"edges": [], "doc": []}',
        encoding="utf-8",
    )
    out_base = tmp_path / "out" / "deliverable"
    captured = {}

    def fake_render(understanding, title, out_path, **kwargs):
        captured["repair_backend"] = kwargs.get("repair_backend")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("<html><svg id=\"x\"></svg></html>", encoding="utf-8")
        return out_path

    with patch("graphitect.deliver.archify_adapter.render", side_effect=fake_render):
        with pytest.raises(SystemExit):
            main(
                [
                    "deliver",
                    str(understanding_file),
                    "--title",
                    "Test",
                    "-o",
                    str(out_base),
                    "--no-repair",
                ]
            )

    assert captured["repair_backend"] is None
