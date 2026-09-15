"""Grounding via a full LLM read of the repo - README, git log, commit
messages, existing docs (plan.md §02). Where rationale actually comes from:
Graphify can tell you two modules are connected, not why Redis sits between
them.

This is the one grounding source that fundamentally needs an LLM, which is
also the one place the skill and CLI surfaces genuinely diverge in
capability (plan.md §05):
  - as a skill, the host IS the LLM - `read_fn` dispatches the host's own
    subagents (e.g. Claude Code's Agent tool), same pattern Graphify itself
    uses for semantic extraction when no GEMINI_API_KEY is set.
  - standalone, `read_fn` must be backed by the caller's own
    ANTHROPIC_API_KEY / GEMINI_API_KEY - there's no host LLM to borrow.

Nothing here performs the read itself; ReadFn is the seam both surfaces
implement differently while everything downstream (rubric, questions,
GroundedUnderstanding) stays identical.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import Claim


class ReadFn(Protocol):
    """One full-read pass over a repo, returning extracted claims.

    Implementations differ by surface (host subagent dispatch vs. a direct
    API call) but must return the same shape: raw claims with confidence
    already assessed against what was actually found, ready for the rubric
    in graphitect.synthesize.rubric to mine further or queue a question.
    """

    def __call__(self, repo_path: Path, sources: list[Path]) -> list[Claim]: ...


def default_sources(repo_path: Path) -> list[Path]:
    """README, CHANGELOG, and any docs/specs - the rubric's own search order
    (plan.md §03), collected once so both a skill subagent and a standalone
    LLM call read the same files.
    """
    candidates = [
        repo_path / "README.md",
        repo_path / "CHANGELOG.md",
    ]
    docs_dir = repo_path / "docs"
    if docs_dir.is_dir():
        candidates.extend(sorted(docs_dir.rglob("*.md")))
    return [p for p in candidates if p.exists()]


def ground(repo_path: Path, read_fn: ReadFn) -> list[Claim]:
    return read_fn(repo_path, default_sources(repo_path))
