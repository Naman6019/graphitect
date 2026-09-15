"""Orchestrates the LLM draft step (llm_backend) with the rubric's own
verification (rubric.py) - the draft is never trusted blindly. A "confirmed"
claim citing a repository file gets re-checked against the actual file; if
the cited text isn't really there, the claim is downgraded to inferred
rather than shipping a fabricated citation. A file-less ``code`` citation is
reserved for Graphitect's own computed Graphify facts; user citations remain
the user's own statement.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..models import Claim, Confidence, DesignDocSection, Evidence, GroundedUnderstanding
from .llm_backend import LLMBackend
from .rubric import build_question

# Keep the draft prompt bounded; truncate, don't fail, on a huge repo. 200K
# chars (~50K tokens) comfortably covers a well-documented small-to-medium
# repo's README+CHANGELOG+docs without truncating mid-document - the
# previous 40K limit was confirmed live to truncate git-resume-agent's own
# README+CHANGELOG (44KB combined) before the model ever saw all of it,
# directly undercutting doc richness on exactly the repos this matters most
# for. Still a single fixed number across every backend regardless of its
# actual context window (Gemini's is far larger than a small local Ollama
# model's) - real per-backend tuning is future work, not solved here.
_MAX_CONTEXT_CHARS = 200_000

# How many of the busiest source files (by node count - a proxy for
# architectural significance, the same "god nodes" intuition
# archify_adapter's community-overflow cap uses) get a real code excerpt
# pulled into the prompt, and how much of each. Naman's request (12 Sep
# 2026) for a more in-depth, explanatory doc needed real mechanism-level
# material to explain FROM, not just a flat node/edge list - a node/edge
# summary alone tells the model *what* is connected, never *how* it works.
_MAX_CODE_EXCERPT_FILES = 6
_MAX_CODE_EXCERPT_CHARS = 2_000


def build_context(grounding: dict, repo_path: Path) -> str:
    """Combine Graphify's structure (if present) with repo text (README,
    CHANGELOG, docs) and a handful of real code excerpts into one
    prompt-sized context. Graphify tells the model *what* is connected; the
    repo text and code excerpts are where *why* and *how* actually come from
    (plan.md §02).
    """
    parts: list[str] = []

    if grounding.get("mode") == "graphify":
        nodes = grounding.get("nodes", [])
        edges = grounding.get("edges", [])
        parts.append(f"Structure graph: {len(nodes)} nodes, {len(edges)} edges.")
        node_lines = [f"- {n.get('id')}: {n.get('label', n.get('id'))}" for n in nodes[:200]]
        parts.append("Nodes:\n" + "\n".join(node_lines))
        edge_lines = [
            f"- {e.get('source') or e.get('from')} -> {e.get('target') or e.get('to')}"
            f" ({e.get('relation', e.get('label', 'related')) })"
            for e in edges[:300]
        ]
        parts.append("Edges:\n" + "\n".join(edge_lines))

        # Graphify's own `source_file` field on AST nodes (confirmed live
        # against FundersAI's real graph.json) names exactly which real file
        # backs each node; the files referenced by the most nodes are a
        # reasonable proxy for "central to the architecture" without any
        # extra analysis. Best-effort: a file that no longer exists (stale
        # grounding, a moved file) is silently skipped, same tolerance
        # README/docs reading below already has.
        file_counts: dict[str, int] = {}
        for n in nodes:
            source_file = n.get("source_file")
            if source_file:
                file_counts[source_file] = file_counts.get(source_file, 0) + 1
        top_files = sorted(file_counts, key=file_counts.get, reverse=True)[:_MAX_CODE_EXCERPT_FILES]
        for source_file in top_files:
            f = repo_path / source_file
            if f.is_file():
                excerpt = f.read_text(encoding="utf-8", errors="ignore")[:_MAX_CODE_EXCERPT_CHARS]
                parts.append(f"\n--- code excerpt: {source_file} ---\n{excerpt}")
    elif grounding.get("mode") == "describe":
        parts.append("User's own description:")
        for claim in grounding.get("claims", []):
            parts.append(f"- {claim.get('text')}")

    for name in ("README.md", "CHANGELOG.md"):
        f = repo_path / name
        if f.exists():
            parts.append(f"\n--- {name} ---\n" + f.read_text(encoding="utf-8", errors="ignore"))

    docs_dir = repo_path / "docs"
    if docs_dir.is_dir():
        for doc in sorted(docs_dir.rglob("*.md"))[:10]:
            parts.append(
                f"\n--- {doc.relative_to(repo_path)} ---\n"
                + doc.read_text(encoding="utf-8", errors="ignore")
            )

    context = "\n\n".join(parts)
    if len(context) > _MAX_CONTEXT_CHARS:
        context = context[:_MAX_CONTEXT_CHARS] + "\n\n[... truncated ...]"
    return context


def _citation_note_is_present(text: str, note: str) -> bool:
    """Require the full cited phrase, ignoring only case and whitespace."""
    normalized_note = " ".join(note.casefold().split())
    return bool(normalized_note) and normalized_note in " ".join(text.casefold().split())


def _git_log_contains(repo_path: Path, note: str) -> bool:
    """Verify a commit citation against Git when it has no source file."""
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--oneline", "--format=%h %s"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        return False
    return note.strip() in {line.strip() for line in result.stdout.splitlines()}


def _repository_file(repo_path: Path, evidence_file: str | None) -> Path | None:
    """Resolve a citation only when it remains inside the analyzed repository."""
    if not evidence_file:
        return None
    try:
        target = (repo_path / evidence_file).resolve()
        target.relative_to(repo_path.resolve())
    except ValueError:
        return None
    return target


def _verify_claim(claim: Claim, repo_path: Path) -> Claim:
    """Re-check a "confirmed" claim's own citations against the actual file.
    Downgrades to inferred (with a note) rather than trusting the model's
    say-so - the honesty guarantee has to survive the LLM being wrong about
    its own evidence, not just the mining rubric being right.
    """
    if claim.confidence != Confidence.CONFIRMED:
        return claim

    for evidence in claim.cites:
        if evidence.source == "user":
            continue
        if (
            evidence.source == "code"
            and evidence.file is None
            and evidence.note == "Graphify structural extraction"
        ):
            # The diagram-only fallback carries this exact computed Graphify
            # summary. Every host-authored code citation must name a file.
            continue
        if evidence.source == "git_log" and not evidence.file:
            if evidence.note and _git_log_contains(repo_path, evidence.note):
                continue
        else:
            target = _repository_file(repo_path, evidence.file)
            if target and target.exists() and evidence.note and _citation_note_is_present(
                target.read_text(encoding="utf-8", errors="ignore"), evidence.note
            ):
                continue
        # Citation didn't check out - downgrade rather than ship a
        # fabricated "confirmed" tag.
        return claim.model_copy(
            update={
                "confidence": Confidence.INFERRED,
                "text": f"{claim.text} (citation could not be independently verified)",
            }
        )
    return claim


def verify_understanding(understanding: GroundedUnderstanding, repo_path: Path) -> GroundedUnderstanding:
    """Verify every host- or provider-authored claim before delivery."""
    pending = []
    for section in understanding.doc:
        for i, claim in enumerate(section.claims):
            verified = _verify_claim(claim, repo_path)
            section.claims[i] = verified
            if verified.confidence == Confidence.INFERRED:
                question = build_question(verified, f"{section.heading}::{i}", section.heading, guess=verified.text)
                if question:
                    pending.append(question)

        for tradeoff in section.tradeoffs:
            tradeoff.decision = _verify_claim(tradeoff.decision, repo_path)
            tradeoff.pros = [_verify_claim(c, repo_path) for c in tradeoff.pros]
            tradeoff.cons = [_verify_claim(c, repo_path) for c in tradeoff.cons]

    understanding.pending_questions = pending
    return understanding


def synthesize(
    grounding: dict,
    repo_path: Path,
    backend: LLMBackend,
    *,
    diagram_kind: str = "architecture",
) -> GroundedUnderstanding:
    context = build_context(grounding, repo_path)
    draft = backend.draft(context, diagram_kind=diagram_kind)

    if grounding.get("mode") == "graphify":
        # Use Graphify's real, deterministic AST-extracted nodes/edges for
        # the diagram rather than trusting whatever the LLM's draft
        # invented - the model's summary is prone to dropping/renaming/
        # inventing structure since nothing obliges it to reproduce the real
        # graph faithfully (Naman's point, 12 Sep 2026: "in-depth coverage
        # of graphify - what's connected to what" was the actual goal, not
        # an LLM's approximation of it). The LLM's own nodes/edges draft is
        # discarded entirely here; only its `doc` (rationale claims) survives.
        draft["nodes"] = grounding.get("nodes", [])
        draft["edges"] = grounding.get("edges", [])
        draft["community_labels"] = grounding.get("community_labels", {})

    understanding = GroundedUnderstanding.model_validate(draft)

    if grounding.get("mode") == "graphify":
        # The LLM's related_node_ids referenced ITS OWN invented node ids,
        # which mean nothing now that real Graphify nodes/edges replaced the
        # draft's - drop any that don't exist in the real graph rather than
        # relying on the model having been told (and having obeyed) to cite
        # real ids. A stale reference should disappear, not silently point
        # at a node that no longer exists.
        real_ids = {n["id"] for n in understanding.nodes if "id" in n}
        for section in understanding.doc:
            section.related_node_ids = [rid for rid in section.related_node_ids if rid in real_ids]

    return verify_understanding(understanding, repo_path)


def synthesize_diagram_only(grounding: dict, *, diagram_kind: str = "architecture") -> GroundedUnderstanding:
    """No LLM call at all - the fallback when no backend key is configured.

    The diagram itself needs zero LLM involvement in graphify mode:
    nodes/edges/community_labels are already deterministic Graphify output,
    untouched by the draft step even when an LLM IS available (see
    synthesize() above - the model's own nodes/edges get discarded and
    replaced with these same real ones). Only the narrative `doc` ever
    actually required a model. Rather than failing outright and leaving the
    user with nothing, hand back the real diagram data with one purely
    factual, computed overview claim (node/edge/community counts - no
    interpretation involved, so it's honestly "confirmed" with no
    verification pass needed) - so `deliver` can still produce a full
    interactive diagram (Naman, 12 Sep 2026: "the diagram is a must").

    Only meaningful for graphify-mode grounding - `describe` mode has no
    structural data to fall back on and genuinely needs the LLM to turn a
    free-text description into anything at all; callers should keep failing
    loudly in that case rather than call this with an empty result.
    """
    nodes = grounding.get("nodes", [])
    edges = grounding.get("edges", [])
    communities = grounding.get("communities", {})

    summary = f"{len(nodes)} structural nodes and {len(edges)} relationships were extracted by Graphify"
    if communities:
        summary += f", grouped into {len(communities)} communities"
    summary += ". No LLM backend was available, so this doc is structure-only - no rationale was drafted."

    overview_claim = Claim(
        text=summary,
        confidence=Confidence.CONFIRMED,
        cites=[Evidence(source="code", note="Graphify structural extraction")],
    )

    return GroundedUnderstanding(
        diagram_kind=diagram_kind,
        nodes=nodes,
        edges=edges,
        doc=[DesignDocSection(heading="Overview", claims=[overview_claim])],
        community_labels=grounding.get("community_labels", {}),
    )
