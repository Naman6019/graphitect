from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from graphitect.models import Confidence, GroundedUnderstanding
from graphitect.synthesize.engine import build_context, synthesize, synthesize_diagram_only
from graphitect.synthesize.llm_backend import resolve_backend


class FakeBackend:
    """A canned backend for testing the orchestration without a live API
    call - graphitect never spends the user's API credits in a test.
    """

    def __init__(self, draft: dict):
        self._draft = draft
        self.last_context: str | None = None
        self.last_diagram_kind: str | None = None

    def draft(self, context: str, *, diagram_kind: str) -> dict:
        self.last_context = context
        self.last_diagram_kind = diagram_kind
        return self._draft


def _minimal_draft(claims: list[dict]) -> dict:
    return {
        "diagram_kind": "architecture",
        "nodes": [{"id": "api", "label": "API"}],
        "edges": [],
        "doc": [{"heading": "Technology choices & why", "claims": claims}],
    }


def test_build_context_truncates_rather_than_failing_on_a_huge_repo(tmp_path: Path):
    (tmp_path / "README.md").write_text("x" * 300_000, encoding="utf-8")
    grounding = {"mode": "describe", "claims": []}
    context = build_context(grounding, tmp_path)
    assert len(context) < 300_000
    assert context.endswith("[... truncated ...]")


def test_build_context_does_not_truncate_a_real_well_documented_repo(tmp_path: Path):
    # Regression test for a real live bug: the previous 40K-char limit
    # truncated git-resume-agent's own README (32KB) + CHANGELOG (12KB) -
    # 44KB combined - before the model ever saw all of it, on exactly the
    # kind of well-documented repo this project is supposed to shine on.
    (tmp_path / "README.md").write_text("x" * 32_000, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("y" * 12_000, encoding="utf-8")
    context = build_context({"mode": "describe", "claims": []}, tmp_path)
    assert "[... truncated ...]" not in context
    assert "x" * 32_000 in context
    assert "y" * 12_000 in context


def test_build_context_includes_readme_and_graph_summary(tmp_path: Path):
    (tmp_path / "README.md").write_text("Uses FastAPI for async support.", encoding="utf-8")
    grounding = {
        "mode": "graphify",
        "nodes": [{"id": "api", "label": "API Server"}],
        "edges": [{"from": "api", "to": "db", "label": "SQL"}],
    }
    context = build_context(grounding, tmp_path)
    assert "FastAPI" in context
    assert "api" in context
    assert "1 nodes, 1 edges" in context


def test_build_context_includes_code_excerpts_from_the_busiest_source_files(tmp_path: Path):
    # Naman's request (12 Sep 2026) for a more in-depth, explanatory doc:
    # a flat node/edge list only tells the model WHAT is connected, never
    # HOW anything works - real code excerpts from the most-referenced files
    # (Graphify's own source_file field, confirmed live against FundersAI's
    # real graph.json) give it actual mechanism-level material to explain
    # from, the same way README/CHANGELOG already do for the "why".
    (tmp_path / "worker.py").write_text(
        "def process_job(job):\n    # pulls from the Redis-backed queue\n    ...\n",
        encoding="utf-8",
    )
    grounding = {
        "mode": "graphify",
        "nodes": [
            {"id": "worker_a", "label": "process_job", "source_file": "worker.py"},
            {"id": "worker_b", "label": "enqueue", "source_file": "worker.py"},
            {"id": "misc", "label": "helper", "source_file": "nonexistent.py"},
        ],
        "edges": [],
    }
    context = build_context(grounding, tmp_path)
    assert "code excerpt: worker.py" in context
    assert "Redis-backed queue" in context
    # A stale/missing source_file (nonexistent.py) is skipped, not a crash.
    assert "nonexistent.py" not in context


def test_build_context_limits_code_excerpts_to_the_busiest_files(tmp_path: Path):
    for i in range(10):
        (tmp_path / f"file{i}.py").write_text(f"# file {i}\n", encoding="utf-8")
    # file0 is referenced by far more nodes than the rest - it must make the
    # cut even though there are more distinct files than the excerpt cap.
    nodes = [{"id": f"n{i}", "source_file": "file0.py"} for i in range(20)]
    nodes += [{"id": f"m{i}", "source_file": f"file{i}.py"} for i in range(1, 10)]
    context = build_context({"mode": "graphify", "nodes": nodes, "edges": []}, tmp_path)
    assert "code excerpt: file0.py" in context
    assert context.count("--- code excerpt:") <= 6


def test_synthesize_verifies_a_real_citation_and_keeps_it_confirmed(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "This project uses FastAPI for its async support.\n", encoding="utf-8"
    )
    draft = _minimal_draft(
        [
            {
                "text": "Chose FastAPI for async support",
                "confidence": "confirmed",
                "cites": [{"source": "readme", "file": "README.md", "note": "uses FastAPI for its async support"}],
                "kind": "descriptive",
            }
        ]
    )
    backend = FakeBackend(draft)
    understanding = synthesize({"mode": "describe", "claims": []}, tmp_path, backend)

    claim = understanding.doc[0].claims[0]
    assert claim.confidence == Confidence.CONFIRMED
    assert understanding.pending_questions == []


def test_synthesize_downgrades_a_fabricated_citation(tmp_path: Path):
    (tmp_path / "README.md").write_text("This project does a thing.\n", encoding="utf-8")
    draft = _minimal_draft(
        [
            {
                "text": "Chose FastAPI for async support",
                "confidence": "confirmed",
                # cites a note that does NOT actually appear in README.md
                "cites": [{"source": "readme", "file": "README.md", "note": "definitely uses FastAPI here"}],
                "kind": "descriptive",
            }
        ]
    )
    backend = FakeBackend(draft)
    understanding = synthesize({"mode": "describe", "claims": []}, tmp_path, backend)

    claim = understanding.doc[0].claims[0]
    assert claim.confidence == Confidence.INFERRED
    assert "could not be independently verified" in claim.text
    # a downgraded, descriptive, load-bearing claim should now be askable
    assert len(understanding.pending_questions) == 1


def test_synthesize_rejects_a_citation_with_only_a_trivial_word_overlap(tmp_path: Path):
    (tmp_path / "README.md").write_text("The project has a command-line interface.\n", encoding="utf-8")
    draft = _minimal_draft(
        [
            {
                "text": "Chose FastAPI for scalability",
                "confidence": "confirmed",
                "cites": [
                    {
                        "source": "readme",
                        "file": "README.md",
                        "note": "the team chose FastAPI for scalability",
                    }
                ],
            }
        ]
    )

    understanding = synthesize({"mode": "describe", "claims": []}, tmp_path, FakeBackend(draft))

    assert understanding.doc[0].claims[0].confidence == Confidence.INFERRED


def test_synthesize_verifies_file_backed_code_citations(tmp_path: Path):
    (tmp_path / "service.py").write_text(
        "# Persists workflow state in the repository store.\n", encoding="utf-8"
    )
    draft = _minimal_draft(
        [
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
                "text": "The store is horizontally scalable",
                "confidence": "confirmed",
                "cites": [
                    {
                        "source": "code",
                        "file": "service.py",
                        "note": "The store is horizontally scalable.",
                    }
                ],
            },
        ]
    )

    understanding = synthesize({"mode": "describe", "claims": []}, tmp_path, FakeBackend(draft))

    assert understanding.doc[0].claims[0].confidence == Confidence.CONFIRMED
    assert understanding.doc[0].claims[1].confidence == Confidence.INFERRED


def test_synthesize_verifies_a_git_log_citation_without_a_source_file(tmp_path: Path):
    commit = "abc1234 Use FastAPI for async request handling"
    draft = _minimal_draft(
        [
            {
                "text": "Chose FastAPI for async request handling",
                "confidence": "confirmed",
                "cites": [{"source": "git_log", "note": commit}],
            }
        ]
    )

    with patch(
        "graphitect.synthesize.engine.subprocess.run",
        return_value=SimpleNamespace(stdout=f"{commit}\n", returncode=0),
    ) as run:
        understanding = synthesize({"mode": "describe", "claims": []}, tmp_path, FakeBackend(draft))

    assert understanding.doc[0].claims[0].confidence == Confidence.CONFIRMED
    assert run.call_args.kwargs["encoding"] == "utf-8"
    assert run.call_args.kwargs["errors"] == "replace"


def test_synthesize_verifies_tradeoff_decision_and_pros_and_cons_citations(tmp_path: Path):
    # Tradeoff.decision/pros/cons are full Claims (Naman's request, 12 Sep
    # 2026, for real pros/cons tables) and must get the exact same
    # fabrication safety net as section.claims - a "confirmed" pro whose
    # citation doesn't actually check out must be downgraded here too, not
    # only for the flat claims list.
    (tmp_path / "README.md").write_text(
        "This project uses FastAPI for its async support.\n", encoding="utf-8"
    )
    draft = _minimal_draft([])
    draft["doc"][0]["tradeoffs"] = [
        {
            "decision": {
                "text": "Chose FastAPI for async support",
                "confidence": "confirmed",
                "cites": [{"source": "readme", "file": "README.md", "note": "uses FastAPI for its async support"}],
                "kind": "descriptive",
            },
            "alternatives_considered": ["Django", "Flask"],
            "pros": [
                {
                    "text": "Native async/await support",
                    "confidence": "confirmed",
                    # deliberately no word-overlap with the real README text
                    # above - a fabricated citation, not a real one.
                    "cites": [{"source": "readme", "file": "README.md", "note": "quantum flux capacitor calibration"}],
                    "kind": "descriptive",
                }
            ],
            "cons": [{"text": "Smaller ecosystem", "confidence": "inferred", "kind": "descriptive"}],
        }
    ]
    backend = FakeBackend(draft)
    understanding = synthesize({"mode": "describe", "claims": []}, tmp_path, backend)

    tradeoff = understanding.doc[0].tradeoffs[0]
    assert tradeoff.decision.confidence == Confidence.CONFIRMED  # real citation, survives
    assert tradeoff.pros[0].confidence == Confidence.INFERRED  # fabricated citation, downgraded
    assert "could not be independently verified" in tradeoff.pros[0].text
    assert tradeoff.cons[0].confidence == Confidence.INFERRED  # untouched, was already inferred


def test_synthesize_keeps_user_citations_confirmed(tmp_path: Path):
    draft = _minimal_draft(
        [
            {
                "text": "Author confirmed this was deliberate",
                "confidence": "confirmed",
                "cites": [{"source": "user", "note": "yes, deliberate"}],
                "kind": "descriptive",
            }
        ]
    )
    backend = FakeBackend(draft)
    understanding = synthesize({"mode": "describe", "claims": []}, tmp_path, backend)
    assert understanding.doc[0].claims[0].confidence == Confidence.CONFIRMED


def test_synthesize_rejects_a_fileless_host_code_citation(tmp_path: Path):
    draft = _minimal_draft(
        [
            {
                "text": "Host says this code path is horizontally scalable",
                "confidence": "confirmed",
                "cites": [{"source": "code", "note": "host-invented evidence"}],
            }
        ]
    )

    understanding = synthesize({"mode": "describe", "claims": []}, tmp_path, FakeBackend(draft))

    assert understanding.doc[0].claims[0].confidence == Confidence.INFERRED


def test_synthesize_rejects_a_citation_outside_the_repository(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("# The source phrase is here.\n", encoding="utf-8")
    draft = _minimal_draft(
        [
            {
                "text": "Host cites an external file",
                "confidence": "confirmed",
                "cites": [
                    {"source": "code", "file": "../outside.py", "note": "The source phrase is here."}
                ],
            }
        ]
    )

    understanding = synthesize({"mode": "describe", "claims": []}, repo, FakeBackend(draft))

    assert understanding.doc[0].claims[0].confidence == Confidence.INFERRED


def test_synthesize_result_validates_as_grounded_understanding(tmp_path: Path):
    draft = _minimal_draft([])
    backend = FakeBackend(draft)
    understanding = synthesize({"mode": "describe", "claims": []}, tmp_path, backend)
    assert isinstance(understanding, GroundedUnderstanding)


def test_resolve_backend_raises_without_any_key():
    with pytest.raises(RuntimeError, match="No LLM backend"):
        resolve_backend(anthropic_key=None, gemini_key=None)


def test_synthesize_diagram_only_keeps_the_real_graphify_diagram_data():
    # Naman's requirement (12 Sep 2026): "the diagram is a must" - with no
    # LLM key at all, deliver must still be able to produce a full
    # interactive diagram, since nodes/edges/community_labels are already
    # deterministic Graphify output with zero LLM involvement even on the
    # normal synthesize() path.
    grounding = {
        "mode": "graphify",
        "nodes": [{"id": "a", "label": "A", "community": 1}, {"id": "b", "label": "B", "community": 2}],
        "edges": [{"from": "a", "to": "b"}],
        "communities": {"1": ["a"], "2": ["b"]},
        "community_labels": {"1": "Alpha", "2": "Beta"},
    }
    understanding = synthesize_diagram_only(grounding, diagram_kind="architecture")
    assert understanding.nodes == grounding["nodes"]
    assert understanding.edges == grounding["edges"]
    assert understanding.community_labels == {"1": "Alpha", "2": "Beta"}
    assert understanding.diagram_kind == "architecture"


def test_synthesize_diagram_only_never_calls_any_backend():
    # No `backend` parameter exists at all - the whole point is this path
    # makes zero network/API calls, verified structurally rather than by
    # mocking (there's nothing to mock; no backend argument can be passed).
    import inspect

    assert "backend" not in inspect.signature(synthesize_diagram_only).parameters


def test_synthesize_diagram_only_produces_one_honest_factual_overview_claim():
    grounding = {
        "mode": "graphify",
        "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "edges": [{"from": "a", "to": "b"}],
    }
    understanding = synthesize_diagram_only(grounding)
    assert len(understanding.doc) == 1
    claim = understanding.doc[0].claims[0]
    assert claim.confidence == Confidence.CONFIRMED  # a real, computed count - not a guess
    assert "3 structural nodes" in claim.text
    assert "1 relationships" in claim.text
    assert claim.cites  # still carries a citation like everything else in the doc


def test_synthesize_replaces_llm_invented_nodes_with_real_graphify_nodes(tmp_path: Path):
    # Naman's core point (12 Sep 2026): the diagram should reflect Graphify's
    # real "what's connected to what", not an LLM's invented approximation
    # of it. The draft's own nodes/edges must never survive graphify-mode
    # synthesis, even though the LLM was asked to produce some.
    draft = _minimal_draft([])
    draft["nodes"] = [{"id": "llm_invented_node", "label": "Made Up"}]
    draft["edges"] = [{"from": "llm_invented_node", "to": "llm_invented_node"}]
    backend = FakeBackend(draft)

    real_grounding = {
        "mode": "graphify",
        "nodes": [{"id": "real_cli_sync", "label": "sync", "community": 1}],
        "edges": [],
        "community_labels": {"1": "Automatic sync"},
    }
    understanding = synthesize(real_grounding, tmp_path, backend)

    assert understanding.nodes == real_grounding["nodes"]
    assert understanding.community_labels == {"1": "Automatic sync"}
    assert "llm_invented_node" not in [n.get("id") for n in understanding.nodes]


def test_synthesize_drops_stale_related_node_ids_after_node_substitution(tmp_path: Path):
    draft = _minimal_draft(
        [{"text": "x", "confidence": "confirmed", "cites": [], "kind": "descriptive"}]
    )
    draft["doc"][0]["related_node_ids"] = ["llm_invented_node", "real_cli_sync"]
    draft["nodes"] = [{"id": "llm_invented_node", "label": "Made Up"}]
    backend = FakeBackend(draft)

    real_grounding = {
        "mode": "graphify",
        "nodes": [{"id": "real_cli_sync", "label": "sync"}],
        "edges": [],
    }
    understanding = synthesize(real_grounding, tmp_path, backend)

    assert understanding.doc[0].related_node_ids == ["real_cli_sync"]


def test_synthesize_leaves_llm_nodes_alone_for_non_graphify_grounding(tmp_path: Path):
    # describe/full-read modes have no independent "real" graph to defer to -
    # the LLM's own nodes are the only structure available, so they must not
    # be discarded the way graphify-mode's are.
    draft = _minimal_draft([])
    backend = FakeBackend(draft)
    understanding = synthesize({"mode": "describe", "claims": []}, tmp_path, backend)
    assert understanding.nodes == draft["nodes"]


def test_resolve_backend_prefers_ollama_local_over_other_keys():
    pytest.importorskip("openai", reason="graphitect[ollama] extra not installed")
    from graphitect.synthesize.llm_backend import OllamaBackend

    backend = resolve_backend(
        anthropic_key="anthropic-key", gemini_key="gemini-key", ollama_local=True
    )
    assert isinstance(backend, OllamaBackend)


def test_ollama_backend_defaults_to_local_url_without_a_key():
    pytest.importorskip("openai", reason="graphitect[ollama] extra not installed")
    from graphitect.synthesize.llm_backend import OllamaBackend

    backend = OllamaBackend()
    assert str(backend._client.base_url) == "http://localhost:11434/v1/"


def test_ollama_backend_defaults_to_cloud_url_with_a_key():
    pytest.importorskip("openai", reason="graphitect[ollama] extra not installed")
    from graphitect.synthesize.llm_backend import OllamaBackend

    backend = OllamaBackend(api_key="real-ollama-cloud-key")
    assert str(backend._client.base_url) == "https://ollama.com/v1/"


def test_ollama_backend_respects_explicit_base_url_override():
    pytest.importorskip("openai", reason="graphitect[ollama] extra not installed")
    from graphitect.synthesize.llm_backend import OllamaBackend

    backend = OllamaBackend(base_url="http://192.168.1.50:11434/v1")
    assert "192.168.1.50" in str(backend._client.base_url)


def test_ollama_backend_default_model_differs_between_local_and_cloud():
    # Regression test for a real live bug: local Ollama model names (e.g.
    # llama3.1) don't exist in Ollama Cloud's catalog at all, and Cloud
    # returns a bare 401 for an unrecognized/inaccessible model - which looks
    # identical to a bad API key until you check /v1/models with the same
    # key and see it's actually fine. The two modes must never share a default.
    pytest.importorskip("openai", reason="graphitect[ollama] extra not installed")
    from graphitect.synthesize.llm_backend import OllamaBackend

    local = OllamaBackend()
    cloud = OllamaBackend(api_key="fake-cloud-key")
    assert local._model != cloud._model
    assert local._model == OllamaBackend._LOCAL_DEFAULT_MODEL
    assert cloud._model == OllamaBackend._CLOUD_DEFAULT_MODEL


def test_resolve_backend_preserves_the_cloud_default_model():
    pytest.importorskip("openai", reason="the bundled openai dependency is unavailable")
    from graphitect.synthesize.llm_backend import OllamaBackend

    backend = resolve_backend(
        anthropic_key=None,
        gemini_key=None,
        ollama_key="fake-cloud-key",
    )

    assert isinstance(backend, OllamaBackend)
    assert backend._model == OllamaBackend._CLOUD_DEFAULT_MODEL


def test_ollama_backend_explicit_model_overrides_the_mode_default():
    pytest.importorskip("openai", reason="graphitect[ollama] extra not installed")
    from graphitect.synthesize.llm_backend import OllamaBackend

    backend = OllamaBackend("qwen3.5:397b", api_key="fake-cloud-key")
    assert backend._model == "qwen3.5:397b"
