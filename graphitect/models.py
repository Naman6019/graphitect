"""Shared model for graphitect - see plan.md sections 03/04.

One internal structure produced by Synthesize, mapped two ways downstream:
into archify's own typed IR for the diagram, and into a sourced doc outline
for the write-up. Every claim traces to evidence or is explicitly labeled a
guess - never presented as confident prose either way.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Confidence(str, Enum):
    CONFIRMED = "confirmed"  # direct code fact, or the author said so
    INFERRED = "inferred"    # plausible - must be labeled as such


class Evidence(BaseModel):
    source: Literal["code", "readme", "git_log", "docs", "user"]
    file: str | None = None
    line: int | None = None
    note: str | None = None  # e.g. commit message excerpt, or the user's own answer


class Claim(BaseModel):
    text: str  # "chose FastAPI for async support"
    confidence: Confidence
    cites: list[Evidence] = Field(default_factory=list)
    # "descriptive" = a fact about a past decision, askable via the rubric.
    # "prescriptive" = the tool's own recommendation/suggestion, never askable -
    # asking "did you really mean this suggestion" doesn't make sense (plan.md §03).
    kind: Literal["descriptive", "prescriptive"] = "descriptive"


class Tradeoff(BaseModel):
    """A real pros/cons comparison for one architectural/technical decision -
    what "Tradeoffs & alternatives considered" is actually for, beyond a
    flat claim bullet (Naman's request, 12 Sep 2026, for a more in-depth,
    explanatory doc). `decision`/`pros`/`cons` are full Claims - confidence
    tag + citations, exactly like anything else in the doc - so a table row
    can never quietly carry an unverified assertion the rest of the doc
    wouldn't allow; engine.synthesize() re-verifies all three the same way
    it verifies section.claims. `decision` may restate or elaborate a claim
    already present in this section's own `claims` list - the flat list is
    the quick-scan version, this is the detailed one.

    Not currently wired into the interactive Q&A/pending-question flow
    (scoped out deliberately - see plan.md): an inferred, unresolved
    tradeoff still displays honestly labeled, it just isn't independently
    askable via --answers the way a flat section.claims entry is.
    """

    decision: Claim
    alternatives_considered: list[str] = Field(default_factory=list)
    pros: list[Claim] = Field(default_factory=list)
    cons: list[Claim] = Field(default_factory=list)


class DesignDocSection(BaseModel):
    heading: str  # "Technology choices"
    claims: list[Claim] = Field(default_factory=list)
    related_node_ids: list[str] = Field(default_factory=list)  # cross-links into the archify IR
    tradeoffs: list[Tradeoff] = Field(default_factory=list)


class QuestionOption(BaseModel):
    label: str  # short label, e.g. "Type-hints + Rich dashboards"
    becomes_text: str  # exact confirmed claim text if this option is chosen


class RationaleQuestion(BaseModel):
    """One askable gap, per plan.md §03.

    Only descriptive claims with zero evidence from the mining rubric AND
    load-bearing enough to matter (landed in Technology Choices or Tradeoffs)
    become a RationaleQuestion. Renders identically whether the surface is an
    agent host's structured-question tool or a bare terminal prompt - see
    graphitect.synthesize.questions.
    """

    claim_id: str
    question: str
    options: list[QuestionOption]  # options[0] is always Synthesize's own best guess
    allow_free_text: bool = True


class GroundedUnderstanding(BaseModel):
    diagram_kind: Literal["architecture", "workflow", "sequence", "dataflow", "lifecycle"]
    nodes: list[dict] = Field(default_factory=list)  # mapped into archify's own node schema
    edges: list[dict] = Field(default_factory=list)
    doc: list[DesignDocSection] = Field(default_factory=list)
    pending_questions: list[RationaleQuestion] = Field(default_factory=list)
    # Plain-language Graphify community names (community id -> label), when
    # grounding came from a real graphify run that produced them. Used to
    # name aggregated diagram boxes when there are too many raw nodes for a
    # readable diagram - see archify_adapter._aggregate_by_community. Naman's
    # point (12 Sep 2026): a large codebase's full graph either won't render
    # or renders unreadably, so the diagram falls back to Graphify's own
    # community-detection output instead of an LLM re-inventing a summary.
    community_labels: dict[str, str] = Field(default_factory=dict)


class AgentNarrative(BaseModel):
    """The only portion an installed agent may author.

    Graphitect joins this with deterministic Graphify structure during
    ``graphitect agent compile``. The schema deliberately excludes nodes and
    edges, so a host model cannot alter the diagram it explains.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    doc: list[DesignDocSection] = Field(default_factory=list)
