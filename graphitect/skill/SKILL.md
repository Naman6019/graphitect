---
name: graphitect
description: Generate an evidence-grounded, self-contained interactive architecture report for an existing repository. Use for codebase diagrams, architecture walkthroughs, technical-choice or trade-off explanations, and interview-preparation artifacts; do not use for greenfield architecture proposals.
license: MIT
metadata:
  version: "0.2.0"
---

# Graphitect

Use Graphitect for a repository the user already has when they want a codebase
diagram, project walkthrough, technical-choice explanation, or
interview-preparation artifact.

Do not use it to invent an architecture for a project that does not yet exist.

## Installed-agent mode — no separate API key

When this skill runs inside Codex, Claude Code, or another agent host, use the
host's selected model for Section 2. Graphitect makes no LLM network call in
this mode.

```bash
graphitect agent prepare <path> -o .graphitect-agent
```

Read `.graphitect-agent/agent-context.md`,
`.graphitect-agent/narrative.schema.json`, and any source files needed to
check a claim. Edit only `.graphitect-agent/narrative.json` with detailed
`doc` sections for the project explanation, technical choices, mechanisms,
and trade-offs.

- Graphitect owns the diagram: never add, remove, or rename nodes or edges.
- Mark a claim `confirmed` only with real evidence. A code citation needs a
  repository-relative `file` and a `note` that is a full phrase from that file.
- Do not use `user` evidence in `narrative.json`; only `--answers` may add a
  direct user answer to the final report.
- Mark interpretation, unstated rationale, and recommendations as `inferred`.
- Use only real `related_node_ids` from the prepared grounding.

```bash
graphitect agent compile .graphitect-agent --title <project> -o <project>-graphitect.html
```

This validates host-written citations and writes the single self-contained
HTML artifact. If `.graphitect-agent/graphitect-questions.json` contains
unanswered entries, ask the user, fill each `answer`, then rerun `agent
compile --answers` with that same file. The first compile is still useful:
unresolved claims remain honestly labeled as inferred.

Only use `--no-diagram` when the user explicitly opts out. A diagram-rendering
failure is a command failure because Section 1 is mandatory by default.

## Standalone CLI mode

```bash
graphitect build <path> -o <project>-graphitect.html
```

This single command runs the bundled Graphify analyzer, renders with the
bundled Archify viewer, and writes one HTML file. Do not install or invoke
Graphify or Archify separately.

Outside an agent host, an API key is optional. With `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`, `GOOGLE_API_KEY`, or `OLLAMA_API_KEY`, Section 2 includes
the detailed project explanation. Without a key, Section 1 still renders and
Section 2 honestly states that the narrative was skipped.

## Verify

Confirm that the output contains both named sections and that the embedded
Archify viewer opens, pans/zooms, switches theme, exposes per-block hover
details, and exposes its route and guided-view controls.

Keep claims labeled as confirmed or inferred; never invent a citation,
technology rationale, runtime trace, or provider relationship. A workflow
arrow is a direct static Graphify relationship. A sequence appears only when
Graphify observed consecutive `calls` or `invokes` edges; it is not runtime
telemetry. Preserve the source symbol in hover details when a legacy function
name could be mistaken for provider topology.
