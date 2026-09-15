"""Two renderers, one RationaleQuestion object - plan.md §03.

The skill surface maps a RationaleQuestion straight onto the host's
structured-question tool (e.g. Claude Code's AskUserQuestion) - that mapping
lives in the SKILL.md itself, not here, since it's a host-tool-call, not
Python. This module is the standalone-CLI renderer: a numbered terminal
prompt when interactive, or a JSON file round-trip when not - matching
Graphify's own `--update` re-run convention rather than inventing a new one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..models import Claim, Confidence, Evidence, RationaleQuestion


def ask_interactive(questions: list[RationaleQuestion]) -> dict[str, str]:
    """Numbered terminal prompt with a free-text escape hatch. Returns
    {claim_id: answer_text} - either the chosen option's becomes_text or the
    typed free text, verbatim.
    """
    answers: dict[str, str] = {}
    total = len(questions)
    for i, q in enumerate(questions, start=1):
        print(f"\n[{i}/{total}] {q.question}")
        for j, opt in enumerate(q.options, start=1):
            marker = "  [our best guess]" if j == 1 else ""
            print(f"  {j}) {opt.label}{marker}")
        if q.allow_free_text:
            print(f"  {len(q.options) + 1}) Something else (type your own)")
        try:
            choice = input("> ").strip()
        except EOFError:
            # No real terminal behind stdin despite isatty() saying so (seen
            # under some sandboxed/wrapped shells) - stop asking rather than
            # crash; caller falls back to writing out whatever's left unanswered.
            print("\n(no input available - leaving remaining questions unanswered)")
            break

        if choice.isdigit() and 1 <= int(choice) <= len(q.options):
            answers[q.claim_id] = q.options[int(choice) - 1].becomes_text
        elif q.allow_free_text:
            free_text = choice if not choice.isdigit() else input("Your answer: ").strip()
            answers[q.claim_id] = free_text
        else:
            print("No valid option chosen - leaving this claim inferred.")
    return answers


def write_pending(questions: list[RationaleQuestion], out_path: Path) -> Path:
    """Non-interactive path: don't block CI/piped runs. Write pending
    questions to disk for a human to answer later via `graphitect synthesize
    --answers <file>`. Set each emitted `answer` field to a string, then pass
    the same file back to `--answers`.
    """
    payload = [{**q.model_dump(), "answer": None} for q in questions]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


def read_answers(answers_path: Path) -> dict[str, str]:
    """Load answers written by a human editing the file written by
    write_pending. The preferred format is its question list with an `answer`
    string filled per item. The older {"claim_id": "answer text"} mapping is
    accepted for compatibility.
    """
    payload = json.loads(answers_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        if not all(isinstance(claim_id, str) and isinstance(answer, str) for claim_id, answer in payload.items()):
            raise TypeError("answer mappings must contain string claim IDs and answer text")
        return payload
    if not isinstance(payload, list):
        raise TypeError("answers must be an object or the generated question list")

    answers: dict[str, str] = {}
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("claim_id"), str):
            raise TypeError(f"question {index} must contain a string claim_id")
        answer = item.get("answer")
        if answer is None or answer == "":
            continue
        if not isinstance(answer, str):
            raise TypeError(f"question {index} answer must be a string or null")
        if item["claim_id"] in answers:
            raise ValueError(f"duplicate answer for claim_id {item['claim_id']!r}")
        answers[item["claim_id"]] = answer
    return answers


def apply_answer(claim: Claim, answer_text: str) -> Claim:
    """An answer always confirms - and, if it's free text rather than a
    listed option, *rewrites* claim.text to match what was actually said.
    That's what let a wrong guess (e.g. Phase 0's Word-COM inference) get
    genuinely corrected instead of rubber-stamped (plan.md §03).
    """
    return claim.model_copy(
        update={
            "text": answer_text,
            "confidence": Confidence.CONFIRMED,
            "cites": [*claim.cites, Evidence(source="user", note=answer_text)],
        }
    )


def resolve_questions(
    questions: list[RationaleQuestion],
    claims_by_id: dict[str, Claim],
    pending_path: Path,
    *,
    non_interactive: bool = False,
) -> tuple[dict[str, Claim], list[RationaleQuestion]]:
    """Interactive when attached to a real terminal, otherwise write pending
    questions to disk and leave those claims inferred for this run.

    `non_interactive` is an explicit override for `--non-interactive`/CI use -
    isatty() alone isn't reliable everywhere (some wrapped/sandboxed shells
    report a tty even with stdin redirected from /dev/null), and a dropped
    stdin mid-prompt raises EOFError rather than silently hanging, so this
    always degrades to write_pending for whatever wasn't answered rather than
    crashing.
    """
    if not questions:
        return claims_by_id, []

    if non_interactive or not sys.stdin.isatty():
        write_pending(questions, pending_path)
        return claims_by_id, questions

    answers = ask_interactive(questions)
    for claim_id, answer_text in answers.items():
        claims_by_id[claim_id] = apply_answer(claims_by_id[claim_id], answer_text)
    unanswered = [q for q in questions if q.claim_id not in answers]
    if unanswered:
        write_pending(unanswered, pending_path)
    return claims_by_id, unanswered
