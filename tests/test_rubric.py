from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from graphitect.models import Claim, Confidence
from graphitect.synthesize.rubric import build_question, mine_git_log, mine_readme


def test_mine_readme_finds_matching_line(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "# My Tool\n\nUses Typer for the CLI because of its type-hint driven options.\n",
        encoding="utf-8",
    )
    evidence = mine_readme(tmp_path, ["typer"])
    assert evidence is not None
    assert evidence.source == "readme"
    assert "typer" in evidence.note.lower()


def test_mine_readme_returns_none_when_no_match(tmp_path: Path):
    (tmp_path / "README.md").write_text("# My Tool\n\nDoes a thing.\n", encoding="utf-8")
    assert mine_readme(tmp_path, ["nonexistent-keyword"]) is None


def test_mine_git_log_decodes_utf8_subprocess_output(tmp_path: Path):
    with patch(
        "graphitect.synthesize.rubric.subprocess.run",
        return_value=SimpleNamespace(stdout="abc1234 Add unicode \u2603 support\n"),
    ) as run:
        evidence = mine_git_log(tmp_path, ["unicode"])

    assert evidence is not None
    assert evidence.note == "abc1234 Add unicode \u2603 support"
    assert run.call_args.kwargs["encoding"] == "utf-8"
    assert run.call_args.kwargs["errors"] == "replace"


def test_build_question_skips_prescriptive_claims():
    claim = Claim(
        text="extend the verifier",
        confidence=Confidence.INFERRED,
        kind="prescriptive",
    )
    assert build_question(claim, "id", "Tradeoffs & alternatives considered", guess="x") is None


def test_build_question_skips_non_askable_section():
    claim = Claim(text="what it does", confidence=Confidence.INFERRED, kind="descriptive")
    assert build_question(claim, "id", "Overview", guess="x") is None


def test_build_question_fires_for_descriptive_unresolved_claim_in_askable_section():
    claim = Claim(text="chose X", confidence=Confidence.INFERRED, kind="descriptive")
    q = build_question(claim, "choices::0", "Technology choices & why", guess="best guess")
    assert q is not None
    assert q.options[0].label == "best guess"
