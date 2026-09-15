"""The rationale-mining rubric - plan.md §03.

For each claim: code -> README -> git log -> other docs -> stop. If nothing
found, only *descriptive* claims in a load-bearing section become an askable
RationaleQuestion; *prescriptive* claims (recommendations, "not a stated
plan") stay `inferred` forever by construction - asking "did you really mean
this suggestion" doesn't make sense the same way a factual gap does.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..models import Claim, Confidence, Evidence, QuestionOption, RationaleQuestion

# Only these sections are worth a targeted question - incidental color in
# Overview/Components/Workflows isn't "why" material (plan.md §03).
ASKABLE_SECTIONS = {"Technology choices & why", "Tradeoffs & alternatives considered"}


def search_text(text: str, keywords: list[str]) -> str | None:
    """Return the first line containing any keyword, or None. A real
    implementation would use embeddings/fuzzy match; keyword search is the
    honest floor - it either finds explicit textual evidence or it doesn't,
    with no risk of a false-confident semantic match standing in for a
    citation.
    """
    lowered_keywords = [k.lower() for k in keywords]
    for line in text.splitlines():
        low = line.lower()
        if any(k in low for k in lowered_keywords):
            return line.strip()
    return None


def mine_readme(repo_path: Path, keywords: list[str]) -> Evidence | None:
    readme = repo_path / "README.md"
    if not readme.exists():
        return None
    hit = search_text(readme.read_text(encoding="utf-8", errors="ignore"), keywords)
    return Evidence(source="readme", file="README.md", note=hit) if hit else None


def mine_git_log(repo_path: Path, keywords: list[str]) -> Evidence | None:
    changelog = repo_path / "CHANGELOG.md"
    if changelog.exists():
        hit = search_text(changelog.read_text(encoding="utf-8", errors="ignore"), keywords)
        if hit:
            return Evidence(source="git_log", file="CHANGELOG.md", note=hit)

    try:
        result = subprocess.run(
            ["git", "log", "--all", "--grep", "|".join(keywords), "-i", "--oneline", "-n", "1"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        return None
    hit = result.stdout.strip()
    return Evidence(source="git_log", note=hit) if hit else None


def mine_other_docs(repo_path: Path, keywords: list[str]) -> Evidence | None:
    docs_dir = repo_path / "docs"
    if not docs_dir.is_dir():
        return None
    for doc in sorted(docs_dir.rglob("*.md")):
        hit = search_text(doc.read_text(encoding="utf-8", errors="ignore"), keywords)
        if hit:
            return Evidence(source="docs", file=str(doc.relative_to(repo_path)), note=hit)
    return None


def mine_rationale(repo_path: Path, claim_text: str, keywords: list[str]) -> Evidence | None:
    """Walk the rubric in order, stop at the first hit. Code-level evidence
    (step 1) isn't handled here - that comes pre-attached from Graphify's own
    extraction (EXTRACTED edges) or a direct source read, not a text search.
    """
    for miner in (mine_readme, mine_git_log, mine_other_docs):
        evidence = miner(repo_path, keywords)
        if evidence:
            return evidence
    return None


def build_question(claim: Claim, claim_id: str, section: str, guess: str) -> RationaleQuestion | None:
    """Only queue a question for a descriptive, load-bearing, unresolved claim."""
    if claim.kind != "descriptive" or section not in ASKABLE_SECTIONS:
        return None
    if claim.confidence == Confidence.CONFIRMED:
        return None
    return RationaleQuestion(
        claim_id=claim_id,
        question=f"Why: {claim.text}?",
        options=[
            QuestionOption(label=guess, becomes_text=claim.text),
            QuestionOption(label="No deeper reason", becomes_text=f"{claim.text} (no deliberate reason given)"),
        ],
    )
