import json
from pathlib import Path
from unittest.mock import patch

import pytest

from graphitect.models import Claim, Confidence, QuestionOption, RationaleQuestion
from graphitect.synthesize.questions import (
    apply_answer,
    read_answers,
    resolve_questions,
    write_pending,
)


def _question(claim_id: str) -> RationaleQuestion:
    return RationaleQuestion(
        claim_id=claim_id,
        question="Why X?",
        options=[QuestionOption(label="guess", becomes_text="confirmed: guess")],
    )


def test_apply_answer_confirms_and_rewrites_text():
    claim = Claim(text="original guess", confidence=Confidence.INFERRED)
    updated = apply_answer(claim, "the real reason")
    assert updated.confidence == Confidence.CONFIRMED
    assert updated.text == "the real reason"
    assert updated.cites[-1].source == "user"


def test_generated_pending_questions_file_is_a_valid_answers_round_trip(tmp_path: Path):
    path = tmp_path / "graphitect-questions.json"
    write_pending([_question("choices::0")], path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload[0]["answer"] is None
    assert read_answers(path) == {}  # passing an untouched generated file never crashes

    payload[0]["answer"] = "the real reason"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert read_answers(path) == {"choices::0": "the real reason"}


def test_read_answers_rejects_invalid_answer_shapes(tmp_path: Path):
    path = tmp_path / "answers.json"
    path.write_text(json.dumps([{"claim_id": "choices::0", "answer": 7}]), encoding="utf-8")

    with pytest.raises(TypeError, match="answer must be a string or null"):
        read_answers(path)


def test_resolve_questions_non_interactive_flag_writes_pending_without_touching_stdin(tmp_path: Path):
    # Regression test: a sandboxed shell can report isatty()==True even with
    # stdin redirected from /dev/null, which previously crashed with
    # EOFError instead of degrading to the file-based path. --non-interactive
    # must skip stdin entirely, not just rely on isatty().
    q = _question("choices::0")
    claims = {"choices::0": Claim(text="guess", confidence=Confidence.INFERRED)}
    pending_path = tmp_path / "graphitect-questions.json"

    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True  # simulate the misleading sandboxed case
        result_claims, pending = resolve_questions(
            [q], claims, pending_path, non_interactive=True
        )

    assert pending == [q]
    assert pending_path.exists()
    assert result_claims["choices::0"].confidence == Confidence.INFERRED  # untouched


def test_resolve_questions_eof_mid_prompt_falls_back_to_pending_instead_of_crashing(tmp_path: Path):
    q1, q2 = _question("a"), _question("b")
    claims = {
        "a": Claim(text="guess a", confidence=Confidence.INFERRED),
        "b": Claim(text="guess b", confidence=Confidence.INFERRED),
    }
    pending_path = tmp_path / "graphitect-questions.json"

    with patch("sys.stdin") as mock_stdin, patch("builtins.input", side_effect=EOFError):
        mock_stdin.isatty.return_value = True
        result_claims, pending = resolve_questions([q1, q2], claims, pending_path)

    assert [p.claim_id for p in pending] == ["a", "b"]
    assert pending_path.exists()
