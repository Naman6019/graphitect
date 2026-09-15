"""The one genuinely LLM-dependent piece of Synthesize: turning raw grounding
data (Graphify's nodes/edges, a plain description, or repo text) into
Claim/DesignDocSection objects in the first place.

Two concrete backends for standalone CLI use (`--anthropic` / `--gemini`,
plan.md §05's "what differs standalone" note) plus the seam a skill uses
instead: SkillBackend.draft() is never called in-process at all - the
SKILL.md instructs the host to do this reasoning itself and hand back JSON
matching the same schema, so graphitect's own code never needs to know it's
running inside an agent.
"""

from __future__ import annotations

import json
from typing import Protocol

_CANONICAL_SECTIONS = [
    "Overview",
    "Components & responsibilities",
    "Technology choices & why",
    "Tradeoffs & alternatives considered",
    "Key workflows",
    "Limitations & future work",
]

_SYSTEM_PROMPT = f"""\
You are graphitect's Synthesize step. Given grounding data about a
repository (a structural graph from Graphify, repo text: README, CHANGELOG,
docs, and excerpts of its most central source files), produce a
GroundedUnderstanding: a diagram structure plus a sourced, IN-DEPTH design
doc. A reader should come away understanding HOW the system actually works,
not just a list of facts about it - write substantive claims, not headlines.
For every claim, explain the mechanism, grounded in whatever real evidence
you have. "Uses a queue for background jobs" is a headline; "Background
jobs are pushed onto a Redis-backed queue (worker.py) and processed by a
separate worker process, decoupling slow operations like PDF generation
from the request/response cycle" is a substantive claim. Prefer fewer,
denser claims over many thin ones.

Write this as an interview-ready architecture explanation, not a dependency
inventory. For each applicable section, aim for 3-6 self-contained,
mechanism-level claims. In particular:
- In "Technology choices & why", every load-bearing choice should name the
  technology, its concrete role in this repository, why that role fits the
  system, and one operational consequence. Do not merely repeat a package
  name.
- In "Tradeoffs & alternatives considered", analyze 3-5 significant choices
  when the evidence supports them. Each analysis should name plausible
  alternatives, at least two concrete benefits, and at least one cost,
  constraint, or risk. Keep uncertainty explicit rather than inventing a
  historical decision record.
- In "Key workflows", explain the actual sequence of handoffs, persistence,
  background work, and user-visible result where those are evidenced. A
  workflow claim should let a reader narrate the path without opening the
  source tree.
- Cover security, data boundaries, operations, and failure behavior when
  they are present in the evidence; do not force them into a repository that
  does not implement them.

Use ONLY these section headings, spelled exactly as shown, and only the ones
that actually apply - do not invent your own heading, and do not rename or
paraphrase these:
{chr(10).join(f'  - "{h}"' for h in _CANONICAL_SECTIONS)}
This matters beyond formatting: only claims placed in "Technology choices &
why" or "Tradeoffs & alternatives considered" ever get offered back to the
user as a follow-up question when they're inferred and unresolved. A
genuinely uncertain claim placed under an invented heading instead silently
skips that step - always use the closest matching canonical heading above
rather than inventing a more specific-sounding one.

"Components & responsibilities" and "Key workflows" should read as a real
walkthrough - what a component actually does and why it exists, or how a
workflow actually proceeds step by step - not just a names-only inventory.
Include a claim for every component/workflow that's genuinely worth
explaining, not only the ones with a citable "why" (that stricter load-
bearing bar below still applies to "Technology choices & why" and
"Tradeoffs & alternatives considered" specifically).

Follow the rationale-mining rubric for every claim you write:
1. If the grounding data or repo text directly states a fact or reason,
   the claim is "confirmed" and MUST cite where (source: code/readme/
   git_log/docs, with a file/note).
2. If you cannot find direct evidence but the claim is still worth making,
   mark it "inferred" and say so plainly in the text - never phrase a guess
   as if it were verified.
3. Classify every claim's kind:
   - "descriptive": a fact about a past decision (why X was chosen, what
     tradeoff was made). These are the ones a human could later confirm or
     correct.
   - "prescriptive": your own recommendation for what to do next. These are
     never verifiable against the past, so always "inferred" and never
     phrased as a fact.
4. Only put a claim in "Technology choices & why" or "Tradeoffs &
   alternatives considered" if it's genuinely load-bearing - not every
   detail needs a claim.

For each genuinely significant decision in "Technology choices & why" or
"Tradeoffs & alternatives considered", also add an entry to that section's
`tradeoffs` list - a real pros/cons comparison, not just the flat claim.
`decision` is itself a normal cited/confidence-tagged claim (it may restate
or elaborate a claim already in this section's own `claims`);
`alternatives_considered` names what else was plausible (say plainly if
this is your own inference rather than something the repo actually
discussed); `pros` and `cons` are themselves lists of normal cited/
confidence-tagged claims, never bare strings - do not invent a benefit or
drawback with no basis, mark it "inferred" like any other unverified claim.
Not every claim needs a tradeoffs entry - reserve it for choices substantial
enough to warrant a real comparison.

Never fabricate a citation. Never mark a claim "confirmed" without a
specific source you can point to. When genuinely unsure whether something
is confirmed or inferred, choose inferred.

If a structure graph (from Graphify) is included in the context below, your
own "nodes" and "edges" in the response are discarded and replaced with the
real graph - don't spend effort inventing a diagram structure in that case.
Instead, set each doc section's related_node_ids to actual node ids quoted
from that structure graph (not names you invent) - anything that isn't a
real id from the graph is silently dropped rather than shown as a broken
reference. Code excerpts in the context (when present) are real file
contents from the repository's most central files - use them as your
primary source for HOW claims, citing them as source "code" with the file
path shown in the excerpt's header.
"""

_DRAFT_TOOL_NAME = "emit_grounded_understanding"


class LLMBackend(Protocol):
    def draft(self, context: str, *, diagram_kind: str) -> dict:
        """Return a dict matching GroundedUnderstanding's schema (not yet
        validated - callers run it through GroundedUnderstanding.model_validate).
        """
        ...

    def complete_json(self, system_prompt: str, user_prompt: str, schema: dict, *, tool_name: str) -> dict:
        """General structured-output call: force the model to return a dict
        matching `schema`. `draft()` is just this with a fixed prompt/schema;
        archify_repair's layout-fix loop is the other caller, with a
        different prompt/schema (ArchitectureIR, not GroundedUnderstanding) -
        shared here rather than duplicated per backend.
        """
        ...


def _draft_tool_schema() -> dict:
    from ..models import GroundedUnderstanding

    schema = GroundedUnderstanding.model_json_schema()
    return {
        "name": _DRAFT_TOOL_NAME,
        "description": "Emit the drafted GroundedUnderstanding for this repository.",
        "input_schema": schema,
    }


class AnthropicBackend:
    """Standalone backend backed by the caller's own ANTHROPIC_API_KEY.
    The `anthropic` client ships with Graphitect.
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-5"):
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "AnthropicBackend needs the bundled `anthropic` package; "
                "reinstall Graphitect with `pip install --force-reinstall graphitect`."
            ) from exc
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def draft(self, context: str, *, diagram_kind: str) -> dict:
        schema = _draft_tool_schema()["input_schema"]
        user = f"diagram_kind: {diagram_kind}\n\nGrounding data and repo text:\n\n{context}"
        return self.complete_json(_SYSTEM_PROMPT, user, schema, tool_name=_DRAFT_TOOL_NAME)

    def complete_json(self, system_prompt: str, user_prompt: str, schema: dict, *, tool_name: str) -> dict:
        tool = {"name": tool_name, "description": f"Emit {tool_name}.", "input_schema": schema}
        response = self._client.messages.create(
            model=self._model,
            max_tokens=8192,
            system=system_prompt,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user_prompt}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input
        raise RuntimeError(f"Model did not call the expected tool {tool_name!r} - no output produced.")


class GeminiBackend:
    """Standalone backend backed by the caller's own GEMINI_API_KEY /
    GOOGLE_API_KEY, via Gemini's OpenAI-compatible endpoint (matching how
    Graphify's own gemini extra is structured: openai + tiktoken).
    """

    def __init__(self, api_key: str, model: str = "gemini-3-flash-preview"):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "GeminiBackend needs the bundled `openai` package; "
                "reinstall Graphitect with `pip install --force-reinstall graphitect`."
            ) from exc
        self._client = OpenAI(
            api_key=api_key, base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        self._model = model

    def draft(self, context: str, *, diagram_kind: str) -> dict:
        schema = _draft_tool_schema()["input_schema"]
        user = f"diagram_kind: {diagram_kind}\n\nGrounding data and repo text:\n\n{context}"
        return self.complete_json(_SYSTEM_PROMPT, user, schema, tool_name=_DRAFT_TOOL_NAME)

    def complete_json(self, system_prompt: str, user_prompt: str, schema: dict, *, tool_name: str) -> dict:
        response = self._client.chat.completions.create(
            model=self._model,
            response_format={"type": "json_schema", "json_schema": {"name": tool_name, "schema": schema}},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return json.loads(response.choices[0].message.content)


class OllamaBackend:
    """Local Ollama (no key needed, `http://localhost:11434`) or Ollama
    Cloud (an API key, generous free tier) - same OpenAI-compatible wire
    protocol either way, so one class covers both; only the default
    base_url changes depending on whether a key was given. Mirrors
    git-resume-agent's own confirmed Ollama-first LLM strategy (this
    project's own Phase 0 doc, §02 "Ollama-first" row).

    The bundled `openai` package is used for Ollama's compatibility layer;
    it does not require an OpenAI account.
    """

    # Confirmed live against https://ollama.com/v1/models (12 Sep 2026) - Cloud's
    # catalog is a completely different namespace from local model names (no
    # "llama3.1" on Cloud at all). A model name valid on one is very likely
    # invalid on the other, and Cloud returns a bare 401 for an inaccessible/
    # unrecognized model rather than 404 - which looks exactly like a bad key
    # until you check /v1/models directly with the same key and see it's fine.
    _CLOUD_DEFAULT_MODEL = "gpt-oss:20b"
    _LOCAL_DEFAULT_MODEL = "llama3.1"

    def __init__(
        self,
        model: str | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "OllamaBackend needs the bundled `openai` package; reinstall "
                "Graphitect with `pip install --force-reinstall graphitect`."
            ) from exc
        is_cloud = bool(api_key)
        resolved_base = base_url or ("https://ollama.com/v1" if is_cloud else "http://localhost:11434/v1")
        # Local Ollama ignores the key entirely but the OpenAI client requires
        # a non-empty string; Cloud actually checks it.
        self._client = OpenAI(api_key=api_key or "ollama-local", base_url=resolved_base)
        self._model = model or (self._CLOUD_DEFAULT_MODEL if is_cloud else self._LOCAL_DEFAULT_MODEL)

    def draft(self, context: str, *, diagram_kind: str) -> dict:
        schema = _draft_tool_schema()["input_schema"]
        user = f"diagram_kind: {diagram_kind}\n\nGrounding data and repo text:\n\n{context}"
        return self.complete_json(_SYSTEM_PROMPT, user, schema, tool_name=_DRAFT_TOOL_NAME)

    def complete_json(self, system_prompt: str, user_prompt: str, schema: dict, *, tool_name: str) -> dict:
        # Unlike Gemini's stricter json_schema mode, Ollama's OpenAI-compat
        # layer support for response_format varies by model, so the schema
        # is embedded directly in the prompt as the more portable path -
        # every model can at least follow instructions in plain text even
        # if it can't honor a strict schema constraint.
        system_with_schema = (
            f"{system_prompt}\n\nRespond with ONLY a JSON object matching this schema "
            f"exactly - no markdown fences, no explanation before or after:\n{json.dumps(schema)}"
        )
        response = self._client.chat.completions.create(
            model=self._model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_with_schema},
                {"role": "user", "content": user_prompt},
            ],
        )
        return json.loads(response.choices[0].message.content)


def resolve_backend(
    *,
    anthropic_key: str | None,
    gemini_key: str | None,
    ollama_key: str | None = None,
    ollama_base_url: str | None = None,
    ollama_model: str | None = None,
    ollama_local: bool = False,
) -> LLMBackend:
    """Standalone CLI resolution order: Ollama (key, or `--ollama-local` for
    a locally-running server with no key at all) first - free tier, no
    account needed for local use - then Anthropic, then Gemini, then fail
    loudly. Matches Graphify's own "no other keys are read" honesty rule:
    never silently fall back to a key or endpoint the user didn't ask for.
    """
    if ollama_key or ollama_local:
        return OllamaBackend(
            # Preserve None so OllamaBackend can select the correct default
            # for Cloud (gpt-oss:20b) versus a local server (llama3.1).
            model=ollama_model,
            api_key=ollama_key,
            base_url=ollama_base_url,
        )
    if anthropic_key:
        return AnthropicBackend(anthropic_key)
    if gemini_key:
        return GeminiBackend(gemini_key)
    raise RuntimeError(
        "No LLM backend available. Set OLLAMA_API_KEY (Ollama Cloud), pass "
        "--ollama-local for a locally-running Ollama server, or set ANTHROPIC_API_KEY / "
        "GEMINI_API_KEY - or run graphitect as a Claude Code/Cursor/Codex skill instead, "
        "where the host's own reasoning does this step for free (plan.md §05)."
    )
