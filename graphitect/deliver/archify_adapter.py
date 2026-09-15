"""Maps a GroundedUnderstanding into archify's real architecture IR, then
shells out to the `archify` CLI to validate and render it.

The componentType classification and grid row/col assignment are the two
things the original plan sketch didn't account for (plan.md §04). Both are
heuristics here, deliberately simple - a real implementation should let
Synthesize's own LLM pass override _classify_component with better judgment
when grounding evidence (e.g. Graphify's file_type/source_file) disagrees.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import warnings
from collections import Counter
from pathlib import Path
from typing import Literal

from ..models import GroundedUnderstanding
from ..synthesize.llm_backend import LLMBackend
from . import archify_repair
from .archify_ir import ArchitectureIR, Component, Connection, GridLayout, GuidedView, Meta

# Archify is part of the Graphitect wheel. It intentionally has no npm runtime
# dependencies, so the bundled sources run directly with Node 18+ and never
# trigger a network install.
_BUNDLED_ARCHIFY_PATH = (
    Path(__file__).resolve().parents[1] / "_vendor" / "archify" / "bin" / "archify.mjs"
)

_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "frontend": ("component", "page", "view", ".tsx", ".jsx", "ui/", "frontend/"),
    "database": ("table", "repository", "db", "postgres", "sqlite", "supabase", ".sql"),
    "security": ("auth", "jwt", "oauth", "gate", "verifier"),
    "messagebus": ("queue", "sqs", "kafka", "pubsub", "event bus"),
    "cloud": ("s3", "cdn", "vercel", "cloudfront", "lambda", "cloud run", "k3s", "ec2"),
    "external": ("hook", "human", "external", "third-party", "llm_client", "portfolio_site"),
}


def _classify_component(node: dict) -> str:
    """First-pass heuristic classifier - see module docstring."""
    haystack = " ".join(
        str(node.get(k, "")) for k in ("id", "label", "source_file", "file_type")
    ).lower()
    for archify_type, keywords in _TYPE_KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            return archify_type
    return "backend"  # default: most graph nodes in a service repo are backend logic


def _assign_grid(nodes: list[dict], edges: list[dict], cols: int = 5) -> dict[str, tuple[int, int]]:
    """Row = longest-path depth from a source node (topological layering).
    Col = stable order within a row, by first-seen index. Deterministic, not
    pretty - a real layout pass would minimize edge crossings within a row.
    """
    node_ids = [n["id"] for n in nodes]
    incoming: dict[str, set[str]] = {nid: set() for nid in node_ids}
    for e in edges:
        src, dst = e.get("from") or e.get("source"), e.get("to") or e.get("target")
        if src in incoming and dst in incoming:
            incoming[dst].add(src)

    depth: dict[str, int] = {}

    def _depth_of(nid: str, _seen: frozenset[str] = frozenset()) -> int:
        if nid in depth:
            return depth[nid]
        if nid in _seen:  # cycle guard
            return 0
        parents = incoming.get(nid, set())
        d = 0 if not parents else 1 + max(_depth_of(p, _seen | {nid}) for p in parents)
        depth[nid] = d
        return d

    for nid in node_ids:
        _depth_of(nid)

    by_depth: dict[int, list[str]] = {}
    for nid in node_ids:
        by_depth.setdefault(depth[nid], []).append(nid)

    # A depth with more than `cols` members used to wrap via `col % cols`,
    # which silently placed multiple nodes in the exact same (row, col) cell
    # once a depth exceeded `cols` nodes - confirmed live against FundersAI's
    # real graph, where depth-0 alone had more than 5 nodes and archify's
    # layout validator rejected the resulting "c0"/"c10" overlap. Each depth
    # now claims as many grid rows as it needs (ceil(count / cols)), and the
    # next depth's rows start after all of those - no two nodes ever share
    # a cell.
    positions: dict[str, tuple[int, int]] = {}
    next_row = 0
    for d in sorted(by_depth):
        ids_at_depth = by_depth[d]
        rows_needed = -(-len(ids_at_depth) // cols)  # ceil division
        for i, nid in enumerate(ids_at_depth):
            positions[nid] = (next_row + i // cols, i % cols)
        next_row += rows_needed
    return positions


def _estimate_box_size(labels: list[str]) -> tuple[int, int]:
    """A uniform box size wide enough to fit the longest label, rather than
    archify's own 120px grid default which is only right for short labels.

    ~7px/char is a rough estimate for archify's default UI font at its
    default size - not measured against the actual rendered font metrics
    (that would need a real text-measurement pass, out of scope here), but
    confirmed live to fix the specific "label wider than component" failures
    this heuristic was built to address, on a real 12-node graph with labels
    up to "Portfolio Description Agent" (~178px measured by archify itself).
    Uniform rather than per-component sizing: keeps every box in a shared
    grid column the same width, avoiding the inter-column overlaps a
    variable-width box would otherwise risk.
    """
    longest = max((len(label) for label in labels), default=0)
    width = max(120, longest * 7 + 40)
    return width, 60


# archify's own authoring-contract.md recommends "6-12 primary components" -
# above that, a diagram either fails archify's showcase layout checks or is
# just unreadable regardless. Mirrors Graphify's own graph.json/export html
# fallback to an aggregated community view once a raw graph gets too big to
# render meaningfully (confirmed live during Phase 1 against a 7,885-node
# real graph), except the trigger here is "readable diagram" (~15 boxes),
# not "would this crash a force-directed graph renderer" (Graphify's own
# threshold there is 5,000 nodes - a completely different concern).
_MAX_RAW_COMPONENTS = 15

# Keep the default report readable while retaining a separate complete
# relationship explorer. The cap is deliberately above the 6-12 primary
# components Archify recommends, so the overview can still give every
# connected component one representative relationship on a 15-box graph.
_OVERVIEW_MAX_CONNECTIONS = 18

# Purely mechanical spacing retries render() falls back to when there's no
# LLM repair_backend - confirmed live (12 Sep 2026) that a genuinely small
# 5-node graph can still fail archify's validator on tight label spacing
# alone. Confirmed NOT to help on its own, though: archify's default
# connector-label placement turned out to sit at a FIXED offset from the
# FROM component regardless of how much room widening gapY actually opens
# up (the exact same overlapping label rect at every multiplier tried) - so
# this is combined with _apply_suggested_label_fixes below, not relied on
# alone.
_SPACING_RETRY_MULTIPLIERS = (1.0, 1.75, 2.5)

# How many times render()'s no-repair-backend path retries the mechanical
# label-fix at a single spacing level before giving up and escalating.
# Confirmed live (12 Sep 2026) on a real dense large graph (FundersAI,
# aggregated to 15 boxes / ~35 connections): fixing one label overlap
# reveals or shifts another - archify's validator doesn't necessarily
# surface every issue in one pass - so this can take several rounds to
# fully settle. Each fix only ever pins a connection's labelAt to a fixed
# point (never undoes an earlier fix), so this is guaranteed to converge
# within at most one round per connection that ever needs fixing, not
# oscillate - and each round is a cheap local subprocess call, no LLM
# involved, so a generous budget costs nothing but a little wall time.
_MAX_LABEL_FIX_ROUNDS = 20

# archify's `layout/constraint` (label-overlap) diagnostics carry no
# structured `subject`/`evidence` identifying which connection is at fault
# (confirmed live, 12 Sep 2026 - unlike `clean-flow/edge-through-node`,
# which does) - only the free-text message names the label text and the
# component it overlaps, and often a concrete "Suggested fix: labelAt
# [x, y]". These parse that message well enough to apply the fix directly,
# no LLM needed - archify's own validator already computed the exact
# answer, this just has to read it.
_LABEL_OVERLAP_RE = re.compile(r'Label "([^"]+)" overlaps component "([^"]+)"')
_LABEL_AT_RE = re.compile(r"labelAt \[(-?[\d.]+),\s*(-?[\d.]+)\]")
_GENERIC_COMMUNITY_RE = re.compile(
    r"^(?:community|cluster)\s*[-_#]?\s*\d+$", re.IGNORECASE
)
_TEST_PATH_PARTS = {"test", "tests", "__tests__", "spec", "specs", "fixtures"}


def _normalized_source_file(node: dict) -> str:
    return str(node.get("source_file") or "").replace("\\", "/").strip("/")


def _is_test_node(node: dict) -> bool:
    path = _normalized_source_file(node).lower()
    if not path:
        return False
    parts = path.split("/")
    filename = parts[-1]
    return any(part in _TEST_PATH_PARTS for part in parts) or filename.startswith(
        ("test_", "test-", ".test.", ".spec.")
    )


def _dominant_source_file(nodes: list[dict]) -> str:
    production = [node for node in nodes if not _is_test_node(node)]
    evidence = production or nodes
    paths = [_normalized_source_file(node) for node in evidence]
    paths = [path for path in paths if path]
    return Counter(paths).most_common(1)[0][0] if paths else ""


def _humanize_identifier(value: str) -> str:
    value = re.sub(r"\.(?:py|tsx?|jsx?|json|ya?ml)$", "", value, flags=re.IGNORECASE)
    words = re.sub(r"[_-]+", " ", value).strip().split()
    acronyms = {
        "adk": "ADK",
        "ai": "AI",
        "api": "API",
        "ats": "ATS",
        "db": "DB",
        "http": "HTTP",
        "llm": "LLM",
        "ui": "UI",
    }
    return " ".join(acronyms.get(word.lower(), word.capitalize()) for word in words)


def _has_meaningful_community_label(label: str | None, key: str) -> bool:
    if not label:
        return False
    cleaned = str(label).strip()
    return bool(cleaned) and cleaned.lower() != key.lower() and not _GENERIC_COMMUNITY_RE.fullmatch(
        cleaned
    )


def _label_suffix(nodes: list[dict]) -> str:
    """A concise, source-grounded tie-breaker for duplicate labels."""
    filename = _dominant_source_file(nodes).rsplit("/", 1)[-1]
    stem = re.sub(r"\.[^.]+$", "", filename).lower()
    if stem not in {"", "__init__", "app", "index", "main", "page", "route"}:
        return _humanize_identifier(stem)

    for node in nodes:
        symbol = re.sub(r"\(.*", "", str(node.get("label") or "")).strip()
        if symbol and len(symbol) <= 40:
            return _humanize_identifier(symbol)
    return "component"


def _disambiguate_component_labels(
    label_of: dict[str, str], members_by_key: dict[str, list[dict]], keys: list[str]
) -> None:
    """Keep rendered component labels unique without inventing an LLM name."""
    keys_by_label: dict[str, list[str]] = {}
    for key in keys:
        keys_by_label.setdefault(label_of[key], []).append(key)

    existing = set(label_of.values())
    for label, duplicate_keys in keys_by_label.items():
        if len(duplicate_keys) < 2:
            continue

        suffixes = [_label_suffix(members_by_key.get(key, [])) for key in duplicate_keys]
        duplicate_suffixes = Counter(suffixes)
        for index, (key, suffix) in enumerate(zip(duplicate_keys, suffixes), start=1):
            if duplicate_suffixes[suffix] > 1:
                suffix = f"component {index}"
            candidate = f"{label} ({suffix})"
            while candidate in existing:
                index += 1
                candidate = f"{label} (component {index})"
            label_of[key] = candidate
            existing.add(candidate)


def _derive_community_label(nodes: list[dict], key: str) -> str:
    """Name a Graphify community from repository evidence, without an LLM."""
    production = [node for node in nodes if not _is_test_node(node)]
    evidence = production or nodes
    paths = [_normalized_source_file(node) for node in evidence]
    paths = [path for path in paths if path]
    dominant_path = _dominant_source_file(evidence)
    filename = dominant_path.rsplit("/", 1)[-1] if dominant_path else ""
    stem = re.sub(r"\.[^.]+$", "", filename).lower()
    corpus = " ".join(
        str(node.get(field) or "") for node in evidence for field in ("id", "label")
    ).lower()

    if stem == "freelance_graph" or "talentos // studio" in corpus:
        return "Studio Pipeline"
    if stem == "graph" and ("pipeline" in corpus or "talentos // careers" in corpus):
        return "Careers Pipeline"

    if "firestore" in dominant_path.lower() or stem == "firestore_store":
        budget_score = sum(
            corpus.count(term)
            for term in ("budget", "run_slot", "claim_run", "reserve", "settle", "release")
        )
        record_score = sum(
            corpus.count(term)
            for term in ("application", "materials", "job", "lead", "client", "profile", "listing")
        )
        if budget_score >= 2 and budget_score > record_score:
            return "Firestore Budgets & Runs"
        if record_score:
            return "Firestore Records"
        return "Firestore"

    if stem in {"notify", "notification", "notifications"}:
        return "Notifications"

    frontend_paths = [path.lower() for path in paths if "/frontend/" in f"/{path.lower()}/"]
    if paths and len(frontend_paths) * 2 >= len(paths):
        if stem == "package":
            return "Frontend Dependencies"
        if any("/app/api/" in f"/{path}" for path in frontend_paths) or stem in {
            "cloud-run",
            "auth-server",
        }:
            return "Frontend API"
        # A community may merely import authentication helpers. Prefer the
        # dominant file's role so a generic UI component does not become auth.
        if "auth" in stem or "firebase" in stem:
            return "Frontend Authentication"
        if stem == "types":
            return "Frontend Domain Models"
        if "dashboard" in stem:
            return _humanize_identifier(stem)
        return "Frontend UI"

    source_paths = [path for path in paths if "/sources/" in f"/{path.lower()}/"]
    if source_paths and len(source_paths) * 2 >= len(paths):
        source_names = {
            "aggregators": "Job Aggregators",
            "ats_boards": "ATS Sources",
            "company_portals": "Company Sources",
            "freelance_boards": "Freelance Sources",
            "profile_sources": "Profile Sources",
            "text_utils": "Source Utilities",
        }
        return source_names.get(stem, "Sources")

    named_files = {
        "agent": "AI Agents",
        "board_scout": "Board Discovery",
        "matching": "Job Matching",
        "models": "Domain Models",
        "pipeline": "Evaluation Pipeline",
        "resume_render": "Resume Generation",
        "review_groups": "Review Workflows",
        "run_progress": "Run Progress",
        "schemas": "Data Contracts",
        "telemetry": "Observability",
    }
    if stem in named_files:
        return named_files[stem]
    if stem not in {"", "__init__", "app", "index", "main", "page", "route"}:
        return _humanize_identifier(stem)

    labels = [str(node.get("label") or "").strip() for node in evidence]
    labels = [label for label in labels if label and len(label) <= 48 and not label.startswith(".")]
    return _humanize_identifier(labels[0]) if labels else key


def _apply_suggested_label_fixes(ir: ArchitectureIR, diagnostics: list[dict]) -> ArchitectureIR | None:
    """Best-effort: returns a copy of `ir` with archify's own suggested
    `labelAt` applied to whichever connection each parseable
    `layout/constraint` diagnostic names, or None if no diagnostic could be
    matched to a real connection (nothing to retry with). A diagnostic that
    doesn't parse, or whose named label doesn't match any real connection,
    is skipped rather than treated as fatal - the caller's own retry loop
    still has other spacing attempts left either way.

    Matches purely by label text, NOT by requiring the overlapped
    component to be one of the connection's own endpoints - confirmed live
    (12 Sep 2026) that this requirement made the fix silently never apply
    at all on a real large graph: a rerouted connection's label can default
    to a position anywhere along its (now much longer) multi-waypoint path,
    landing on and overlapping a totally unrelated third component that
    isn't its `from`/`to` at all - the earlier version's endpoint check
    rejected exactly the one real match available, and the same diagnostic
    kept recurring identically across every retry round because the actual
    offending connection was never touched.

    Also skips connections that already have `label_at` set. Label text
    alone isn't a reliable unique key either - `_aggregate_by_community`
    generates generic labels like "3 connections" that multiple different
    community pairs can share verbatim, confirmed live on a real graph
    (three separate connections all labeled "12 connections"). Without this
    check, once the first match got fixed, every later round kept
    "fixing" that SAME already-fixed connection again (its label still
    equalled label_text) instead of moving on to the next real offender
    sharing that label - the retry loop looked like it was making no
    progress at all when it was actually just stuck re-patching one
    connection repeatedly.
    """
    connections = list(ir.connections)
    changed = False
    for diag in diagnostics:
        if diag.get("code") != "layout/constraint":
            continue
        message = diag.get("message", "")
        overlap_match = _LABEL_OVERLAP_RE.search(message)
        labelat_match = _LABEL_AT_RE.search(message)
        if not overlap_match or not labelat_match:
            continue
        label_text, _component_id = overlap_match.groups()
        x, y = (float(v) for v in labelat_match.groups())
        for i, conn in enumerate(connections):
            if conn.label == label_text and conn.label_at is None:
                connections[i] = conn.model_copy(update={"label_at": (x, y)})
                changed = True
                break
    if not changed:
        return None
    return ir.model_copy(update={"connections": connections})


def _group_by_community(
    nodes: list[dict], community_labels: dict[str, str]
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """The grouping + overflow-folding step of _aggregate_by_community,
    extracted so node_id_remap() can compute the exact same raw-node-id ->
    diagram-component-id mapping used for the actual rendered diagram,
    without duplicating (and risking drifting from) the aggregation logic
    itself. See _aggregate_by_community's docstring for the *why*.

    Returns (community_of, label_of, ordered_component_ids).
    """
    community_of: dict[str, str] = {}
    label_of: dict[str, str] = {}
    member_count: dict[str, int] = {}
    members_by_key: dict[str, list[dict]] = {}
    for n in nodes:
        cid = n.get("community")
        key = f"c{cid}" if cid is not None else f"solo_{n['id']}"
        community_of[n["id"]] = key
        members_by_key.setdefault(key, []).append(n)
        member_count[key] = member_count.get(key, 0) + 1

    for key, members in members_by_key.items():
        cid = members[0].get("community")
        supplied = community_labels.get(str(cid)) if cid is not None else None
        if _has_meaningful_community_label(supplied, key):
            label_of[key] = str(supplied).strip()
        elif cid is None:
            label_of[key] = members[0].get("label", members[0]["id"])
        else:
            label_of[key] = _derive_community_label(members, key)

    all_keys = list(dict.fromkeys(community_of.values()))  # stable order, de-duplicated
    if len(all_keys) > _MAX_RAW_COMPONENTS:
        # Tests often contain more symbols than the production subsystem they
        # exercise. Rank by production members first so test-only communities
        # cannot displace the architecture users are trying to understand.
        production_count = {
            key: sum(not _is_test_node(node) for node in members_by_key[key])
            for key in all_keys
        }
        ranked = sorted(
            all_keys,
            key=lambda key: (production_count[key], member_count[key]),
            reverse=True,
        )
        kept_order = ranked[: _MAX_RAW_COMPONENTS - 1]
        kept = set(kept_order)
        overflow_count = sum(member_count[k] for k in all_keys if k not in kept)
        for nid, key in community_of.items():
            if key not in kept:
                community_of[nid] = "other_components"
        label_of["other_components"] = f"Other components ({overflow_count})"
        all_keys = kept_order + ["other_components"]

    _disambiguate_component_labels(label_of, members_by_key, all_keys)
    return community_of, label_of, all_keys


def _aggregate_by_community(
    nodes: list[dict], edges: list[dict], community_labels: dict[str, str]
) -> tuple[list[dict], list[dict]]:
    """Collapse to one box per Graphify community instead of one box per
    raw AST node, when there are too many of the latter for a readable
    diagram. Real Graphify communities, not an LLM's invented summary -
    exactly Naman's point (12 Sep 2026): a large codebase's full graph
    either won't render or renders unreadably, so fall back to Graphify's
    own community-detection output, which was built to solve exactly this
    problem, rather than asking a model to guess at a smaller structure.

    Nodes without a `community` field (e.g. non-Graphify grounding) each
    become their own single-node "community" - this aggregation only
    meaningfully reduces node count when real community data is present.

    A real codebase can have far more communities than _MAX_RAW_COMPONENTS
    allows for a single diagram - confirmed live against FundersAI's actual
    graph.json: 548 communities, not the 2-3 a small unit-test graph has.
    One box per community isn't enough on its own; if there are still too
    many after that first collapse, keep only the largest (by member count,
    a proxy for architectural significance - the same intuition behind
    Graphify's own "god nodes" ranking) and fold everything else into a
    single "Other components" box, rather than emitting hundreds of boxes
    archify will just reject anyway.
    """
    community_of, label_of, all_keys = _group_by_community(nodes, community_labels)
    members_by_component: dict[str, list[dict]] = {key: [] for key in all_keys}
    for node in nodes:
        component_id = community_of[node["id"]]
        members_by_component.setdefault(component_id, []).append(node)
    agg_nodes = [
        {
            "id": key,
            "label": label_of[key] or key,
            "file_type": "community",
            "source_file": _dominant_source_file(members_by_component[key]),
        }
        for key in all_keys
    ]

    cross_edge_counts: dict[tuple[str, str], int] = {}
    for e in edges:
        src, dst = e.get("from") or e.get("source"), e.get("to") or e.get("target")
        c_src, c_dst = community_of.get(src), community_of.get(dst)
        if not c_src or not c_dst or c_src == c_dst:
            continue  # drop intra-community edges - they're implied by sharing a box
        pair = tuple(sorted((c_src, c_dst)))
        cross_edge_counts[pair] = cross_edge_counts.get(pair, 0) + 1

    agg_edges = [
        {
            "from": a,
            "to": b,
            "label": f"{count} connection" + ("s" if count != 1 else ""),
        }
        for (a, b), count in cross_edge_counts.items()
    ]
    return agg_nodes, agg_edges


def _edge_endpoints(edge: dict) -> tuple[str, str]:
    return (
        str(edge.get("from") or edge.get("source") or ""),
        str(edge.get("to") or edge.get("target") or ""),
    )


def _edge_strength(edge: dict) -> int:
    """Return a deterministic importance proxy for a structural edge.

    Community aggregation turns many raw imports into a label such as
    ``"12 connections"``. Prefer that measured count for the overview;
    preserve the original edge order only as a final stable tie-breaker.
    """
    for key in ("weight", "count"):
        try:
            value = int(edge.get(key, 0))
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    match = re.match(r"\s*(\d+)\s+connections?\b", str(edge.get("label") or ""), re.IGNORECASE)
    return int(match.group(1)) if match else 1


def _select_overview_edges(
    nodes: list[dict], edges: list[dict], *, max_connections: int = _OVERVIEW_MAX_CONNECTIONS
) -> list[dict]:
    """Keep a compact, deterministic structural subset for the default view.

    The complete graph remains available in the Full graph mode. Here, first
    choose each component's strongest incident relationship so no connected
    subsystem disappears, then fill the remaining slots by aggregate edge
    strength. This is evidence-preserving filtering, not an inferred summary.
    """
    if len(edges) <= max_connections:
        return list(edges)

    def sort_key(index: int) -> tuple[int, str, str, str, int]:
        source, target = _edge_endpoints(edges[index])
        first, second = sorted((source, target))
        return (-_edge_strength(edges[index]), first, second, str(edges[index].get("label") or ""), index)

    ranked = sorted(range(len(edges)), key=sort_key)
    selected: set[int] = set()
    for node_id in (str(node.get("id") or "") for node in nodes):
        if not node_id or len(selected) >= max_connections:
            break
        candidate = next(
            (
                index
                for index in ranked
                if node_id in _edge_endpoints(edges[index]) and index not in selected
            ),
            None,
        )
        if candidate is not None:
            selected.add(candidate)

    for index in ranked:
        if len(selected) >= max_connections:
            break
        selected.add(index)

    return [edges[index] for index in ranked if index in selected]


# archify's own grid math (archify/renderers/architecture/grid.mjs,
# resolveComponentPos/DEFAULT_GRID - read directly from its source, not
# inferred from rendered output): pos = origin + [col, row] * (cellSize +
# gap). graphitect never overrides origin, and never used to set gapX
# explicitly either (silently inheriting archify's own default) - both are
# made explicit constants here so _route_around_obstacles's geometry is
# guaranteed to match what archify will actually render, not an assumed
# implicit default that could drift.
_GRID_ORIGIN = (40, 80)
_GRID_GAP_X = 30
_GRID_COLUMNS = 5
_VIEWBOX_MARGIN = 40
_VIEWBOX_LEGEND_RESERVE = 68  # 40px renderer margin + Archify's 28px legend rail

# Workflow views deliberately use a short, direct path. A codebase-wide
# dependency graph is valuable as the Overview/Full rollup, but it is not a
# presentation path and becomes jumpy when replayed one node at a time.
_WORKFLOW_MAX_PATH_NODES = 6
_WORKFLOW_MAX_BRANCHES = 2
_WORKFLOW_RELATION_ORDER = {
    "calls": 0,
    "invokes": 0,
    "uses": 1,
    "imports": 2,
    "depends_on": 2,
    "contains": 3,
    "method": 4,
    "inherits": 5,
}

# A sequence diagram has a stricter evidence threshold than the workflow
# story. It is only useful when Graphify observed an ordered chain of actual
# calls; imports, generic uses, and community links are not request evidence.
_SEQUENCE_MAX_PARTICIPANTS = 6
_SEQUENCE_RELATIONS = frozenset({"calls", "invokes"})


def _route_around_obstacles(
    connections: list[Connection],
    positions: dict[str, tuple[int, int]],
    *,
    box_width: int,
    box_height: int,
    cell_w: int,
    cell_h: int,
    gap_x: int,
    gap_y: int,
    cols: int,
) -> list[Connection]:
    """Deterministic obstacle-avoiding routing for connections that would
    otherwise have to cross through an unrelated component's box - the real
    fix for `clean-flow/edge-through-node` on large aggregated diagrams
    without an LLM (Naman, 12 Sep 2026: "at least the graph should work"
    for big, complex codebases with no key configured).

    The grid layout here has no crossing-avoidance of its own. An earlier
    version of this function decided whether to reroute a connection purely
    by row/column distance ("2+ rows apart always gets a detour") -
    confirmed live to reroute far more connections than actually had
    anything in their way, producing a needlessly busy diagram of long
    side-lane detours where archify's own short default route would have
    been perfectly safe. `is_blocked()` below checks the real geometry
    instead: only reroute when some other component's rectangle actually
    overlaps the bounding box between the two endpoints. Rerouted
    connections travel ONLY through space that is provably always empty,
    regardless of how many components exist:

    - The horizontal strip in the gap between any two adjacent rows (from
      one row's bottom edge to the next row's top edge) contains no
      component at ANY column, since every component in a row shares that
      row's y-range - a horizontal move confined to that strip can never
      cross a box.
    - A vertical "lane" positioned past the last column contains no
      component at any row, for the same reason - a vertical move confined
      to that lane can never cross one either.

    So a connection more than one row apart exits its own box vertically
    (safe - it's leaving through its own column, stopping before the next
    row's box starts), travels the empty inter-row strip out to the lane,
    travels down/up the empty lane, then reverses through the target's own
    inter-row strip into its box - never once crossing box-body height in
    another column. A same-row, far-column connection is simpler: dip into
    the empty strip below (or above) that row and back up, without needing
    the lane at all - no other row lies between two components in the same
    row. (An earlier version of this router connected box centers directly
    through box-body height and was confirmed live to still cross other
    components - this waypoint choice is the actual fix, not a refinement
    of it.) Short, local connections (adjacent row and/or column) are left
    on archify's own default routing, already confirmed to work at that
    scale.
    """
    ox, oy = _GRID_ORIGIN
    step_x = cell_w + gap_x
    step_y = cell_h + gap_y
    # Safely past the last *occupied* column, rather than after the configured
    # grid width. A 5-column grid can have only four used columns; using the
    # phantom fifth column made routes needlessly wide and, before meta.viewBox
    # was authored, put them outside Archify's component-derived canvas.
    rightmost_col = max((col for _row, col in positions.values()), default=cols - 1)
    lane_x = ox + (rightmost_col + 1) * step_x + _VIEWBOX_MARGIN

    def rect_of(row: int, col: int) -> tuple[float, float, float, float]:
        x = ox + col * step_x
        y = oy + row * step_y
        return x, y, x + box_width, y + box_height

    def gap_below(row: int) -> float:
        return oy + row * step_y + box_height + gap_y / 2

    def gap_above(row: int) -> float:
        return gap_below(row - 1)

    def x_mid(col: int) -> float:
        return ox + col * step_x + box_width / 2

    def is_blocked(from_pos: tuple[int, int], to_pos: tuple[int, int]) -> bool:
        """Conservative check: does any OTHER component's rectangle overlap
        the bounding box spanned by the two endpoints? Confirmed live (12
        Sep 2026) that a pure row-distance heuristic ("2+ rows apart always
        needs a detour") reroutes far more connections than actually have
        anything in their way, producing a needlessly cluttered diagram of
        long side-lane detours where a short default route would have been
        perfectly safe. Nothing else even overlapping the rectangle between
        two components means there is nothing for any reasonable route
        between them to cross - safe to leave on archify's own (simpler,
        shorter-looking) default routing.
        """
        from_rect, to_rect = rect_of(*from_pos), rect_of(*to_pos)
        bx1 = min(from_rect[0], to_rect[0])
        by1 = min(from_rect[1], to_rect[1])
        bx2 = max(from_rect[2], to_rect[2])
        by2 = max(from_rect[3], to_rect[3])
        for pos in positions.values():
            if pos == from_pos or pos == to_pos:
                continue
            rx1, ry1, rx2, ry2 = rect_of(*pos)
            if rx1 < bx2 and rx2 > bx1 and ry1 < by2 and ry2 > by1:
                return True
        return False

    # Confirmed live (12 Sep 2026), twice: every connection crossing the
    # SAME row-boundary used the exact same y at first (gap_below(row)
    # depends only on `row`, not on which connection) - their lines
    # coincided into what looked like one flat "highway" with labels
    # floating on it with no distinguishable line to trace. A first fix
    # (a fixed per-connection step, applied greedily as each connection was
    # visited) was also confirmed live to fall short on a real dense graph:
    # 8px apart reads as one solid band at normal zoom, and a boundary with
    # more connections than the step's clamp allows starts recolliding
    # anyway. The real fix needs to know, before assigning any offset, how
    # many connections will actually share each boundary - hence two passes:
    # first tally how many connections use each boundary, then space that
    # boundary's connections evenly across the FULL safe band (not a fixed
    # step), so N connections sharing a corridor are always maximally and
    # evenly separated regardless of how large N is.
    boundary_counts: dict[int, int] = {}

    def count_boundary(key: int) -> None:
        boundary_counts[key] = boundary_counts.get(key, 0) + 1

    decisions: list[
        tuple[Connection, tuple[int, int] | None, tuple[int, int] | None, tuple[int, ...]]
    ] = []
    for conn in connections:
        from_pos = positions.get(conn.from_)
        to_pos = positions.get(conn.to)
        if from_pos is None or to_pos is None or not is_blocked(from_pos, to_pos):
            decisions.append((conn, from_pos, to_pos, ()))
            continue

        from_row, _ = from_pos
        to_row, _ = to_pos
        row_gap = to_row - from_row
        if row_gap == 0:
            boundary_keys = (from_row,)
        elif abs(row_gap) == 1:
            boundary_keys = (min(from_row, to_row),)
        elif row_gap > 0:
            boundary_keys = (from_row, to_row - 1)  # gap_above(to_row) == gap_below(to_row - 1)
        else:
            boundary_keys = (from_row - 1, to_row)
        for key in boundary_keys:
            count_boundary(key)
        decisions.append((conn, from_pos, to_pos, boundary_keys))

    stagger_max = max(0.0, gap_y / 2 - 12)
    boundary_seen: dict[int, int] = {}

    def next_offset(key: int) -> float:
        count = boundary_counts.get(key, 1)
        index = boundary_seen.get(key, 0)
        boundary_seen[key] = index + 1
        if count <= 1:
            return 0.0
        # Evenly spaced from -stagger_max to +stagger_max across all
        # `count` connections sharing this boundary - the more that share
        # it, the closer together they sit, but they never collide and
        # never leave the safe band, however many there are.
        return -stagger_max + index * (2 * stagger_max) / (count - 1)

    routed: list[Connection] = []
    lane_offset = 0
    for conn, from_pos, to_pos, boundary_keys in decisions:
        if not boundary_keys:
            routed.append(conn)  # unknown endpoint, or nothing in the way - archify's default is fine
            continue

        from_row, from_col = from_pos
        to_row, to_col = to_pos
        row_gap = to_row - from_row

        if row_gap == 0:
            # Same row: no other ROW lies between them, so a simple dip
            # into this row's own inter-row gap strip and back is enough -
            # the lane isn't needed.
            y = gap_below(from_row) + next_offset(boundary_keys[0])
            routed.append(
                conn.model_copy(
                    update={
                        "from_side": "bottom",
                        "to_side": "bottom",
                        "via": [(x_mid(from_col), y), (x_mid(to_col), y)],
                    }
                )
            )
            continue

        if abs(row_gap) == 1:
            # Adjacent rows: exactly one shared gap strip lies directly
            # between them - still no need for the lane, just use it.
            y = gap_below(boundary_keys[0]) + next_offset(boundary_keys[0])
            from_side = "bottom" if row_gap > 0 else "top"
            to_side = "top" if row_gap > 0 else "bottom"
            routed.append(
                conn.model_copy(
                    update={
                        "from_side": from_side,
                        "to_side": to_side,
                        "via": [(x_mid(from_col), y), (x_mid(to_col), y)],
                    }
                )
            )
            continue

        # 2+ rows apart: no single gap strip touches both endpoints, so
        # bridge between the two different strips via the empty side lane.
        this_lane_x = lane_x + lane_offset
        # Stagger parallel lane routes so a diagram with many of them
        # doesn't stack every route on the exact same vertical line.
        lane_offset = (lane_offset + 20) % 100
        from_boundary, to_boundary = boundary_keys

        if row_gap > 0:
            from_side, to_side = "bottom", "top"
            from_gap_y = gap_below(from_row) + next_offset(from_boundary)
            to_gap_y = gap_above(to_row) + next_offset(to_boundary)
        else:
            from_side, to_side = "top", "bottom"
            from_gap_y = gap_above(from_row) + next_offset(from_boundary)
            to_gap_y = gap_below(to_row) + next_offset(to_boundary)

        routed.append(
            conn.model_copy(
                update={
                    "from_side": from_side,
                    "to_side": to_side,
                    "via": [
                        (x_mid(from_col), from_gap_y),
                        (this_lane_x, from_gap_y),
                        (this_lane_x, to_gap_y),
                        (x_mid(to_col), to_gap_y),
                    ],
                }
            )
        )
    return routed


def _view_box_for_routes(
    positions: dict[str, tuple[int, int]],
    connections: list[Connection],
    *,
    box_width: int,
    box_height: int,
    cell_w: int,
    cell_h: int,
    gap_x: int,
    gap_y: int,
) -> tuple[int, int]:
    """Fit components, explicit detours, connection labels, and the legend.

    Archify's automatic architecture viewBox intentionally measures component
    and boundary boxes only. Graphitect also authors explicit ``via`` points,
    so its canvas must include those points or a valid side lane is visibly
    cropped. The constants mirror Archify's architecture renderer layout.
    """
    ox, oy = _GRID_ORIGIN
    step_x = cell_w + gap_x
    step_y = cell_h + gap_y
    max_component_x = max(
        (ox + col * step_x + box_width for _row, col in positions.values()), default=ox + box_width
    )
    max_component_y = max(
        (oy + row * step_y + box_height for row, _col in positions.values()), default=oy + box_height
    )
    route_points = [point for connection in connections for point in (connection.via or [])]
    max_route_x = max((point[0] for point in route_points), default=max_component_x)
    max_route_y = max((point[1] for point in route_points), default=max_component_y)
    label_half_width = max(
        (max(30.0, len(connection.label or "") * 4.8 + 10.0) / 2 for connection in connections),
        default=0.0,
    )
    right_padding = max(float(_VIEWBOX_MARGIN), label_half_width + 14.0)
    return (
        max(320, int(max(max_component_x, max_route_x) + right_padding + 0.999)),
        max(240, int(max(max_component_y, max_route_y) + _VIEWBOX_LEGEND_RESERVE + 0.999)),
    )


def node_id_remap(understanding: GroundedUnderstanding) -> dict[str, str]:
    """Maps every raw Graphify node id to the diagram component id it
    actually renders as - itself, unchanged, when node count is at or under
    _MAX_RAW_COMPONENTS (to_architecture_ir never aggregates in that case),
    or its community/"other_components" box id when it does.

    A doc section's `related_node_ids` are set once during Synthesize using
    real raw node ids - correct for citing evidence, but once aggregation
    kicks in on a large graph those exact ids no longer exist as elements in
    the rendered SVG, so a node-ref pill's hover-highlight silently finds
    nothing (a known gap flagged in plan.md, fixed by having doc_compiler
    look elements up via this remap rather than the raw id directly).
    """
    nodes = understanding.nodes
    if len(nodes) <= _MAX_RAW_COMPONENTS:
        return {n["id"]: n["id"] for n in nodes if "id" in n}
    community_of, _, _ = _group_by_community(nodes, understanding.community_labels)
    return community_of


def _story_view_id(heading: str, index: int) -> str:
    """Stable, schema-safe chapter id derived from an existing doc heading."""
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return slug or f"chapter-{index + 1}"


def _component_adjacency(component_ids: set[str], edges: list[dict]) -> dict[str, set[str]]:
    """Undirected adjacency for a walk over exact authored relationships.

    The story viewer separately identifies each hop as forward or reverse.
    This helper only decides whether two consecutive focus nodes have a real
    relationship at all; it never invents a transitive hop from proximity.
    """
    adjacency = {component_id: set() for component_id in component_ids}
    for edge in edges:
        source, target = _edge_endpoints(edge)
        if source in adjacency and target in adjacency:
            adjacency[source].add(target)
            adjacency[target].add(source)
    return adjacency


def _continuous_story_path(
    adjacency: dict[str, set[str]],
    component_ids: set[str],
    *,
    required_id: str | None = None,
    max_nodes: int = 5,
) -> list[str]:
    """Pick a bounded simple path whose every adjacent pair is observed.

    `meta.views.focus` is also Archify's Story Beat order. Supplying a hub
    followed by unrelated neighbours made the viewer honestly report
    grouped/no-direct-link transitions, which reads as a jump. A path keeps
    every beat on a real connection and leaves the viewer to truthfully mark
    that connection's direction.
    """
    eligible = set(component_ids)
    if len(eligible) > 1:
        eligible.discard("other_components")
    if not eligible:
        return []
    if required_id is not None and required_id not in eligible:
        return [required_id] if required_id in component_ids else []

    best: tuple[str, ...] = ()

    def consider(path: list[str]) -> None:
        nonlocal best
        if required_id is not None and required_id not in path:
            return
        candidate = tuple(path)
        if len(candidate) > len(best) or (len(candidate) == len(best) and candidate < best):
            best = candidate

    def visit(current: str, path: list[str]) -> None:
        consider(path)
        if len(path) == max_nodes:
            return
        for neighbour in sorted(adjacency.get(current, set()) & eligible):
            if neighbour not in path:
                visit(neighbour, [*path, neighbour])

    for start in sorted(eligible):
        visit(start, [start])
    return list(best)


def _story_views(
    understanding: GroundedUnderstanding,
    components: list[Component],
    all_edges: list[dict],
) -> list[GuidedView]:
    """Create guided chapters without inferring an execution path.

    LLM-backed reports focus repository components explicitly cited by a doc
    section. Diagram-only reports use connected walks around prominent
    components. In both cases, adjacent Story beats share an actual graph
    relationship rather than jumping among a hub's unrelated neighbours.
    """
    component_ids = {component.id for component in components}
    remap = node_id_remap(understanding)
    adjacency = _component_adjacency(component_ids, all_edges)
    views: list[GuidedView] = []
    seen_paths: set[frozenset[str]] = set()

    for index, section in enumerate(understanding.doc):
        cited_components = set(
            dict.fromkeys(
                remap.get(node_id, node_id)
                for node_id in section.related_node_ids
                if remap.get(node_id, node_id) in component_ids
            )
        )
        path = _continuous_story_path(adjacency, cited_components)
        path_key = frozenset(path)
        if not path or path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        views.append(
            GuidedView(
                id=_story_view_id(section.heading, index),
                label=section.heading,
                focus=path,
                note=f"Follows directly observed relationships cited in the {section.heading} explanation.",
            )
        )
        if len(views) == 5:
            return views

    if views:
        return views

    # The aggregation catch-all can touch almost every community. It is useful
    # in the map but a bad story anchor because its focus would dim nothing.
    # Prefer named components whenever any are available.
    anchors = [component for component in components if component.id != "other_components"] or components
    component_by_id = {component.id: component for component in components}
    ranked = sorted(
        anchors,
        key=lambda component: (-len(adjacency[component.id]), component.label.lower()),
    )
    for index, component in enumerate(ranked[:3]):
        path = _continuous_story_path(
            adjacency,
            set(component_by_id),
            required_id=component.id,
        )[:5]
        path_key = frozenset(path)
        if not path or path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        views.append(
            GuidedView(
                id=f"component-{index + 1}",
                label=component.label,
                focus=path,
                note="Follows a connected path through directly observed architecture relationships.",
            )
        )
    return views


def _workflow_relation_key(edge: dict) -> tuple[int, str, str, str]:
    """Keep workflow selection deterministic while preferring executable links."""
    source, target = _edge_endpoints(edge)
    relation = str(edge.get("relation") or edge.get("label") or "relates to").lower()
    return (_WORKFLOW_RELATION_ORDER.get(relation, 99), relation, source, target)


def _workflow_source_label(node: dict) -> str:
    """A compact, code-grounded subtitle for a workflow node."""
    source_file = _normalized_source_file(node)
    if not source_file:
        return "Observed source component"
    return source_file.rsplit("/", 1)[-1]


def _workflow_display_label(node: dict) -> str:
    """Turn a raw code symbol into a readable, bounded diagram label."""
    raw = str(node.get("label") or node.get("id") or "")
    # A source helper named ``function_ollama_*`` is not, by itself, proof
    # that the deployed workflow uses Ollama. Keep the exact symbol in the
    # hover card and label the diagram block by its code-level responsibility.
    if raw.casefold().startswith("function_ollama_"):
        return "LLM Provider Adapter"
    raw = re.sub(r"^function_", "", raw, flags=re.IGNORECASE)
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw)
    words = re.sub(r"[^A-Za-z0-9]+", " ", words).strip().split()
    label_words: list[str] = []
    for word in words:
        candidate = " ".join([*label_words, word])
        if label_words and len(candidate) > 20:
            break
        label_words.append(word)
    return " ".join(label_words).title() or "Observed Component"


def workflow_hover_details(understanding: GroundedUnderstanding) -> dict[str, dict[str, str]]:
    """Return source-grounded hover details for the selected workflow path.

    These details are embedded only in Graphitect's report iframe.  The
    delivered Archify artifact remains byte-for-byte unchanged and validates
    against Archify's workflow schema on its own.
    """
    path, path_edges = _workflow_path(understanding)
    node_by_id = {str(node["id"]): node for node in understanding.nodes if node.get("id")}
    details: dict[str, dict[str, str]] = {}
    for index, node_id in enumerate(path):
        node = node_by_id[node_id]
        raw_symbol = str(node.get("label") or node_id)
        source_file = _normalized_source_file(node)
        if index == 0:
            summary = "Entry in the selected direct code path."
        elif index == len(path) - 1:
            summary = "Endpoint of the selected direct code path."
        else:
            incoming = str(path_edges[index - 1].get("relation") or "relationship")
            outgoing = str(path_edges[index].get("relation") or "relationship")
            summary = (
                f"Reached through a direct {incoming} relationship and continues "
                f"through a direct {outgoing} relationship."
            )
        if raw_symbol.casefold().startswith("function_ollama_"):
            summary = (
                "Code-level LLM provider adapter. Its historical source name is not "
                "evidence that a particular provider is used."
            )
        detail = {"symbol": raw_symbol, "summary": summary}
        if source_file:
            detail["source"] = source_file
        details[f"step-{index + 1}"] = detail
    return details


def _workflow_node_width(node: dict) -> int:
    """Leave enough room for exact code symbols without making a giant card."""
    label = _workflow_display_label(node)
    return min(220, max(132, len(label) * 8 + 32))


def _workflow_seed_ids(understanding: GroundedUnderstanding, node_ids: set[str]) -> list[str]:
    """Prioritise components the explanation calls a workflow, if available."""
    workflow_sections = [
        section for section in understanding.doc if section.heading.lower() == "key workflows"
    ]
    ordered_sections = [*workflow_sections, *understanding.doc]
    seeds = [
        node_id
        for section in ordered_sections
        for node_id in section.related_node_ids
        if node_id in node_ids
    ]
    return list(dict.fromkeys(seeds))


def _choose_workflow_edge(
    candidates: list[dict], seen_nodes: set[str], *, incoming: bool
) -> dict | None:
    valid = []
    for edge in candidates:
        source, target = _edge_endpoints(edge)
        next_node = source if incoming else target
        if next_node and next_node not in seen_nodes:
            valid.append(edge)
    return min(valid, key=_workflow_relation_key) if valid else None


def _workflow_path(understanding: GroundedUnderstanding) -> tuple[list[str], list[dict]]:
    """Find one short connected code path without inferring runtime behavior.

    The path follows only direct Graphify edges. It is intentionally not a
    global longest path: a six-node slice can be read in presentation mode,
    while a repository-wide path would be both unstable and misleading.
    """
    node_by_id = {str(node.get("id") or ""): node for node in understanding.nodes}
    node_by_id = {
        node_id: node for node_id, node in node_by_id.items() if node_id and not _is_test_node(node)
    }
    if not node_by_id:
        raise ValueError("workflow story needs at least one non-test code node")

    incoming: dict[str, list[dict]] = {node_id: [] for node_id in node_by_id}
    outgoing: dict[str, list[dict]] = {node_id: [] for node_id in node_by_id}
    for edge in understanding.edges:
        source, target = _edge_endpoints(edge)
        if source in node_by_id and target in node_by_id and source != target:
            outgoing[source].append(edge)
            incoming[target].append(edge)
    for edges in [*incoming.values(), *outgoing.values()]:
        edges.sort(key=_workflow_relation_key)

    cited_seeds = _workflow_seed_ids(understanding, set(node_by_id))
    ranked_nodes = sorted(
        node_by_id,
        key=lambda node_id: (
            -(len(incoming[node_id]) + len(outgoing[node_id])),
            node_id,
        ),
    )
    seeds = list(dict.fromkeys([*cited_seeds, *ranked_nodes]))
    best_path: list[str] = []
    best_edges: list[dict] = []
    best_score: tuple[int, int, int] = (-1, -1, -1)

    for seed in seeds:
        path = [seed]
        path_edges: list[dict] = []
        seen = {seed}

        # A caller of a cited component makes a better beginning than the
        # component itself, when Graphify observed one. Then extend forward.
        first = _choose_workflow_edge(incoming[seed], seen, incoming=True)
        if first is not None:
            source, _ = _edge_endpoints(first)
            path.insert(0, source)
            path_edges.insert(0, first)
            seen.add(source)

        while len(path) < _WORKFLOW_MAX_PATH_NODES:
            next_edge = _choose_workflow_edge(outgoing[path[-1]], seen, incoming=False)
            if next_edge is None:
                break
            _, target = _edge_endpoints(next_edge)
            path.append(target)
            path_edges.append(next_edge)
            seen.add(target)

        # A sink-only cited component may still have a useful direct caller.
        if len(path) == 1:
            next_edge = _choose_workflow_edge(outgoing[seed], seen, incoming=False)
            if next_edge is not None:
                _, target = _edge_endpoints(next_edge)
                path.append(target)
                path_edges.append(next_edge)

        relation_quality = -sum(_workflow_relation_key(edge)[0] for edge in path_edges)
        score = (
            sum(node_id in cited_seeds for node_id in path),
            len(path),
            relation_quality,
        )
        if score > best_score:
            best_path, best_edges, best_score = path, path_edges, score

    if len(best_path) >= 2:
        return best_path, best_edges

    # A graph with no cited anchors or executable chain still deserves a
    # workflow-like view if Graphify observed any relationship at all.
    for source in ranked_nodes:
        if outgoing[source]:
            edge = outgoing[source][0]
            _, target = _edge_endpoints(edge)
            return [source, target], [edge]
    raise ValueError("workflow story needs at least one direct code relationship")


def to_workflow_spec(understanding: GroundedUnderstanding, title: str) -> dict:
    """Build a compact Archify workflow artifact from direct graph evidence.

    This is intentionally a separate diagram type from the architecture
    rollup. Its main path is a continuous, bounded sequence of observed
    edges; it never turns community aggregates or transitive guesses into a
    narrated flow.
    """
    if understanding.diagram_kind != "architecture":
        raise ValueError(
            f"to_workflow_spec only handles diagram_kind='architecture', got "
            f"{understanding.diagram_kind!r}"
        )

    path, path_edges = _workflow_path(understanding)
    node_by_id = {str(node["id"]): node for node in understanding.nodes if node.get("id")}
    path_set = set(path)
    node_ids = {node_id: f"step-{index + 1}" for index, node_id in enumerate(path)}
    last_col = 5 if len(path) > 1 else 0

    def column(index: int) -> int:
        return round(index * last_col / (len(path) - 1)) if len(path) > 1 else 0

    nodes = [
        {
            "id": node_ids[node_id],
            "lane": "path",
            "col": column(index),
            "type": _classify_component(node_by_id[node_id]),
            "label": _workflow_display_label(node_by_id[node_id]),
            "sublabel": _workflow_source_label(node_by_id[node_id]),
            "width": _workflow_node_width(node_by_id[node_id]),
        }
        for index, node_id in enumerate(path)
    ]
    edges = [
        {
            "id": f"path-{index}",
            "from": node_ids[source],
            "to": node_ids[target],
            "label": str(edge.get("relation") or "observed relationship"),
            "variant": "emphasis" if index == 1 else "default",
            "role": "main",
        }
        for index, (edge, source, target) in enumerate(
            zip(path_edges, path, path[1:]), start=1
        )
    ]

    # Add two genuine, directly linked supporting nodes at most. They give
    # the workflow a second lane without turning it back into a dense map.
    branch_count = 0
    for index, node_id in enumerate(path):
        if branch_count == _WORKFLOW_MAX_BRANCHES:
            break
        for edge in sorted(understanding.edges, key=_workflow_relation_key):
            source, target = _edge_endpoints(edge)
            other = target if source == node_id else source if target == node_id else ""
            if not other or other in path_set or other not in node_by_id:
                continue
            if _is_test_node(node_by_id[other]) or not _normalized_source_file(node_by_id[other]):
                continue
            branch_count += 1
            branch_id = f"dependency-{branch_count}"
            nodes.append(
                {
                    "id": branch_id,
                    "lane": "dependencies",
                    "col": column(index),
                    "type": _classify_component(node_by_id[other]),
                    "label": _workflow_display_label(node_by_id[other]),
                    "sublabel": _workflow_source_label(node_by_id[other]),
                    "width": _workflow_node_width(node_by_id[other]),
                }
            )
            edges.append(
                {
                    "id": f"branch-{branch_count}",
                    "from": node_ids[node_id] if source == node_id else branch_id,
                    "to": branch_id if source == node_id else node_ids[node_id],
                    "label": str(edge.get("relation") or "observed relationship"),
                    "variant": "dashed",
                    "role": "branch",
                }
            )
            break

    main_path = [node_ids[node_id] for node_id in path]
    phases = [
        {"id": "entry", "label": "Entry", "fromCol": 0, "toCol": min(1, last_col)},
        {
            "id": "trace",
            "label": "Observed code path",
            "fromCol": min(2, last_col),
            "toCol": min(3, last_col),
            "variant": "emphasis",
        },
        {
            "id": "endpoint",
            "label": "Endpoint",
            "fromCol": min(4, last_col),
            "toCol": last_col,
            "variant": "dashed",
        },
    ]
    return {
        "schema_version": 2,
        "diagram_type": "workflow",
        "meta": {
            "title": f"{title} · observed code path",
            "animation": "trace",
            "visual_preset": "signal-flow",
            "quality_profile": "showcase",
            "views": [
                {
                    "id": "traced-path",
                    "label": "Traced code path",
                    "focus": main_path,
                    "note": "Follow each direct Graphify relationship in order; no transitive links are added.",
                }
            ],
        },
        "lanes": [
            {"id": "path", "label": "Observed code path"},
            {"id": "dependencies", "label": "Direct dependencies"},
        ],
        "phases": phases,
        "mainPath": main_path,
        "nodes": nodes,
        "edges": edges,
        "cards": [
            {
                "dot": "cyan",
                "title": "Evidence boundary",
                "items": [
                    "Every arrow is a direct Graphify relationship.",
                    "Node subtitles identify the observed source file.",
                ],
            }
        ],
    }


def render_workflow_story(
    understanding: GroundedUnderstanding,
    title: str,
    out_path: Path,
    *,
    archify_bin: str | None = None,
    auto_install: bool = True,
) -> Path:
    """Render the compact, presentation-safe workflow with bundled Archify."""
    bin_cmd = _resolve_archify_bin(archify_bin, auto_install=auto_install)
    spec_path = out_path.with_suffix(".workflow.json")
    spec_path.write_text(json.dumps(to_workflow_spec(understanding, title), indent=2), encoding="utf-8")
    report = archify_repair.run_deliver_json(
        bin_cmd, spec_path, out_path, diagram_type="workflow"
    )
    if report.get("ok"):
        return out_path
    raise subprocess.CalledProcessError(
        1,
        [*bin_cmd, "deliver", "workflow", str(spec_path), str(out_path)],
        output=json.dumps(report),
    )


def _sequence_relation(edge: dict) -> str:
    return str(edge.get("relation") or edge.get("label") or "").strip().lower()


def _is_observed_static_call(edge: dict) -> bool:
    """Require extracted call evidence when Graphify provides confidence.

    Graphify can add inferred symbol-resolution links to its graph. They are
    useful in the architecture explorer, but a sequence must not turn them
    into an apparently observed call trace.
    """
    if _sequence_relation(edge) not in _SEQUENCE_RELATIONS:
        return False
    confidence = str(edge.get("confidence") or "").strip().upper()
    context = str(edge.get("context") or "").strip().lower()
    return (not confidence or confidence == "EXTRACTED") and (not context or context == "call")


def _sequence_path(understanding: GroundedUnderstanding) -> tuple[list[str], list[dict]]:
    """Find one bounded, ordered chain of direct static call edges.

    This deliberately does *not* infer a runtime request path. A sequence is
    offered only when Graphify found at least two consecutive ``calls`` or
    ``invokes`` relationships between non-test symbols.
    """
    node_by_id = {str(node.get("id") or ""): node for node in understanding.nodes}
    node_by_id = {
        node_id: node for node_id, node in node_by_id.items() if node_id and not _is_test_node(node)
    }
    if not node_by_id:
        raise ValueError("sequence trace needs non-test code nodes")

    outgoing: dict[str, list[dict]] = {node_id: [] for node_id in node_by_id}
    for edge in understanding.edges:
        source, target = _edge_endpoints(edge)
        if (
            source in node_by_id
            and target in node_by_id
            and source != target
            and _is_observed_static_call(edge)
        ):
            outgoing[source].append(edge)
    for edges in outgoing.values():
        edges.sort(key=_workflow_relation_key)

    cited_seeds = set(_workflow_seed_ids(understanding, set(node_by_id)))
    starts = sorted(
        node_by_id,
        key=lambda node_id: (
            node_id not in cited_seeds,
            -(len(outgoing[node_id])),
            node_id,
        ),
    )
    best_path: list[str] = []
    best_edges: list[dict] = []
    best_score = (-1, -1, -1)

    for start in starts:
        path = [start]
        path_edges: list[dict] = []
        seen = {start}
        while len(path) < _SEQUENCE_MAX_PARTICIPANTS:
            next_edge = _choose_workflow_edge(outgoing[path[-1]], seen, incoming=False)
            if next_edge is None:
                break
            _, target = _edge_endpoints(next_edge)
            path.append(target)
            path_edges.append(next_edge)
            seen.add(target)

        if len(path_edges) < 2:
            continue
        score = (
            sum(node_id in cited_seeds for node_id in path),
            len(path),
            -sum(_workflow_relation_key(edge)[0] for edge in path_edges),
        )
        if score > best_score:
            best_path, best_edges, best_score = path, path_edges, score

    if len(best_edges) < 2:
        raise ValueError(
            "sequence trace needs two consecutive direct Graphify calls or invocations"
        )
    return best_path, best_edges


def to_sequence_spec(understanding: GroundedUnderstanding, title: str) -> dict:
    """Build an evidence-gated Archify sequence from direct static calls.

    The explicit subtitle and card prevent readers from mistaking source
    analysis for request telemetry, timing data, or a captured trace.
    """
    if understanding.diagram_kind != "architecture":
        raise ValueError(
            f"to_sequence_spec only handles diagram_kind='architecture', got "
            f"{understanding.diagram_kind!r}"
        )

    path, path_edges = _sequence_path(understanding)
    node_by_id = {str(node["id"]): node for node in understanding.nodes if node.get("id")}
    participant_ids = {node_id: f"step-{index + 1}" for index, node_id in enumerate(path)}
    message_start_y = 170
    message_gap = 58
    view_box_height = max(480, message_start_y + len(path_edges) * message_gap + 100)
    view_box_width = max(720, len(path) * 180)

    messages = [
        {
            "id": f"call-{index}",
            "from": participant_ids[source],
            "to": participant_ids[target],
            "y": message_start_y + (index - 1) * message_gap,
            "label": _sequence_relation(edge),
            "variant": "emphasis",
        }
        for index, (edge, source, target) in enumerate(
            zip(path_edges, path, path[1:]), start=1
        )
    ]
    return {
        "schema_version": 1,
        "diagram_type": "sequence",
        "meta": {
            "title": f"{title} · observed static call sequence",
            "subtitle": "Direct Graphify calls/invocations; not runtime telemetry.",
            "viewBox": [view_box_width, view_box_height],
            "column_fit": "spread",
            "animation": "trace",
            "visual_preset": "signal-flow",
            "quality_profile": "showcase",
            "legend": {
                "mode": "auto",
                "entries": {"emphasis": {"label": "direct static call"}},
            },
            "views": [
                {
                    "id": "observed-static-call-sequence",
                    "label": "Observed call chain",
                    "focus": [participant_ids[node_id] for node_id in path],
                    "note": "Each arrow is a direct extracted Graphify calls/invokes relationship.",
                }
            ],
        },
        "participants": [
            {
                "id": participant_ids[node_id],
                "type": _classify_component(node_by_id[node_id]),
                "label": _workflow_display_label(node_by_id[node_id]),
            }
            for node_id in path
        ],
        "messages": messages,
        "activations": [
            {
                "participant": participant_ids[node_id],
                "from": message_start_y + index * message_gap - 6,
                "to": min(
                    view_box_height - 22,
                    message_start_y + (index + 1) * message_gap + 6,
                ),
                "type": _classify_component(node_by_id[node_id]),
            }
            for index, node_id in enumerate(path[1:])
        ],
        "cards": [
            {
                "dot": "cyan",
                "title": "Evidence boundary",
                "items": [
                    "Every arrow is a direct extracted Graphify calls/invokes edge.",
                    "No runtime request, response, timing, or trace is implied.",
                ],
            }
        ],
    }


def render_sequence_trace(
    understanding: GroundedUnderstanding,
    title: str,
    out_path: Path,
    *,
    archify_bin: str | None = None,
    auto_install: bool = True,
) -> Path:
    """Render the optional evidence-gated sequence with bundled Archify."""
    bin_cmd = _resolve_archify_bin(archify_bin, auto_install=auto_install)
    spec_path = out_path.with_suffix(".sequence.json")
    spec_path.write_text(json.dumps(to_sequence_spec(understanding, title), indent=2), encoding="utf-8")
    report = archify_repair.run_deliver_json(
        bin_cmd, spec_path, out_path, diagram_type="sequence"
    )
    if report.get("ok"):
        return out_path
    raise subprocess.CalledProcessError(
        1,
        [*bin_cmd, "deliver", "sequence", str(spec_path), str(out_path)],
        output=json.dumps(report),
    )


def to_architecture_ir(
    understanding: GroundedUnderstanding,
    title: str,
    *,
    spacing_multiplier: float = 1.0,
    view: Literal["story", "overview", "full"] = "full",
) -> ArchitectureIR:
    """`spacing_multiplier` widens the vertical gap between grid rows beyond
    the normal default - the only knob render() has to make the layout more
    forgiving without an LLM. Confirmed live (12 Sep 2026): even a genuinely
    small, non-aggregated 5-node graph can fail archify's validator with
    "label overlaps component" (archify's own default auto-placement for a
    connector's label sometimes lands inside the FROM box when the row gap
    is tight) - a `layout/constraint` failure, not the large-graph
    `clean-flow/edge-through-node` crossing issue the LLM repair loop
    targets. Since "no LLM key configured" must not mean "no diagram"
    (Naman, 12 Sep 2026: "the diagram is a must"), render() retries this
    purely mechanically with progressively more room before giving up.
    """
    if understanding.diagram_kind != "architecture":
        raise ValueError(
            f"to_architecture_ir only handles diagram_kind='architecture', got "
            f"{understanding.diagram_kind!r}"
        )

    if view not in {"story", "overview", "full"}:
        raise ValueError(f"view must be 'story', 'overview', or 'full', got {view!r}")

    nodes, all_edges = understanding.nodes, understanding.edges
    if len(nodes) > _MAX_RAW_COMPONENTS:
        nodes, all_edges = _aggregate_by_community(nodes, all_edges, understanding.community_labels)

    # Both tabs use the same component positions. Filtering overview edges
    # must not rewrite the dependency layers or make the two maps disagree.
    positions = _assign_grid(nodes, all_edges, cols=_GRID_COLUMNS)
    # Story shares Overview's compact structural subset. Its additional value
    # is guided focus, not a second dense map.
    edges = _select_overview_edges(nodes, all_edges) if view in {"story", "overview"} else all_edges
    labels = [n.get("label", n["id"]) for n in nodes]
    box_width, box_height = _estimate_box_size(labels)

    components = [
        Component(
            id=n["id"],
            type=_classify_component(n),
            label=n.get("label", n["id"]),
            sublabel=n.get("sublabel"),
            row=positions[n["id"]][0],
            col=positions[n["id"]][1],
            size=(box_width, box_height),
        )
        for n in nodes
    ]
    connections = [
        Connection(
            id=e.get("id") or f"{e.get('from') or e.get('source')}-{e.get('to') or e.get('target')}",
            **{"from": e.get("from") or e.get("source")},
            to=e.get("to") or e.get("target"),
            label=e.get("label"),
        )
        for e in edges
    ]
    gap_y = int(max(60, box_height + 20) * spacing_multiplier)
    connections = _route_around_obstacles(
        connections,
        positions,
        box_width=box_width,
        box_height=box_height,
        cell_w=box_width + 30,
        cell_h=box_height,
        gap_x=_GRID_GAP_X,
        gap_y=gap_y,
        cols=_GRID_COLUMNS,
    )
    view_box = _view_box_for_routes(
        positions,
        connections,
        box_width=box_width,
        box_height=box_height,
        cell_w=box_width + 30,
        cell_h=box_height,
        gap_x=_GRID_GAP_X,
        gap_y=gap_y,
    )
    return ArchitectureIR(
        meta=Meta(
            title=title,
            viewBox=view_box,
            animation="trace" if view == "story" else "none",
            visual_preset="signal-flow" if view == "story" else None,
            views=_story_views(understanding, components, all_edges) if view == "story" else [],
        ),
        layout=GridLayout(
            cols=_GRID_COLUMNS,
            cellW=box_width + 30,
            cellH=box_height,
            gapX=_GRID_GAP_X,
            # Wider than archify's own 40px default - confirmed live that a
            # labeled connection between two vertically-adjacent grid rows
            # otherwise has nowhere to sit without overlapping one of the
            # two component boxes it connects (multiple "label overlaps
            # component" failures against a real 12-node graph).
            gapY=gap_y,
        ),
        components=components,
        connections=connections,
    )


def _resolve_archify_bin(archify_bin: str | None, *, auto_install: bool) -> list[str]:
    """Return the explicit override or Graphitect's bundled Archify runtime.

    ``auto_install`` remains in the signature for API compatibility with the
    earlier bridge, but is intentionally ignored: a Graphitect run must never
    download another tool at runtime.
    """
    if archify_bin:
        # shlex.split handles both a single executable ("archify", once on
        # PATH) and a multi-token invocation ("node C:\path\to\archify.mjs").
        # posix=False is required on Windows: shlex's default POSIX mode
        # treats backslash as an escape character and silently mangles
        # Windows paths (confirmed live - "node C:\Users\...\archify.mjs"
        # became "C:UsersnamanOneDrive..." with every backslash-letter pair
        # eaten as a fake escape sequence).
        return shlex.split(archify_bin, posix=False)

    if not _BUNDLED_ARCHIFY_PATH.exists():
        raise FileNotFoundError(
            "bundled Archify runtime is missing from this Graphitect installation"
        )

    node = shutil.which("node")
    if not node:
        raise FileNotFoundError(
            "Graphitect includes Archify, but Node.js 18+ is required to run "
            "the bundled renderer"
        )
    return [node, str(_BUNDLED_ARCHIFY_PATH)]


def render(
    understanding: GroundedUnderstanding,
    title: str,
    out_path: Path,
    *,
    archify_bin: str | None = None,
    auto_install: bool = True,
    repair_backend: LLMBackend | None = None,
    max_repair_iterations: int = 3,
    view: Literal["story", "overview", "full"] = "full",
) -> Path:
    """Write the archify IR, then shell out to `archify deliver` to render it.

    Archify ships inside Graphitect and is never installed or downloaded at
    runtime. ``auto_install`` is accepted only for backward compatibility.

    When `repair_backend` is given, a rejected layout (archify's own strict
    validator - dense real-world graphs routinely fail on edges crossing
    unrelated components) is handed to archify_repair's LLM loop instead of
    failing outright: it patches the IR's routing/label-position fields
    against archify's real structured diagnostics and retries, up to
    `max_repair_iterations` total attempts. The repair is optional: provider
    failures and unresolved repair diagnostics fall back to the deterministic
    renderer, so an LLM quota response can never abort `graphitect build`.

    Without a backend, there's no LLM available to patch anything, but "no
    key configured" must still not mean "no diagram" (Naman, 12 Sep 2026:
    "the diagram is a must") - confirmed live that even a small, non-
    aggregated graph can fail archify's validator on tight label spacing
    alone (see to_architecture_ir's spacing_multiplier docstring), so this
    retries the deterministic layout a few times with progressively more
    room before finally raising CalledProcessError for the caller to handle.
    """
    bin_cmd = _resolve_archify_bin(archify_bin, auto_install=auto_install)
    ir_path = out_path.with_suffix(".architecture.json")

    if repair_backend is not None:
        ir = to_architecture_ir(understanding, title, view=view)
        try:
            report = archify_repair.repair_and_deliver(
                ir, bin_cmd, ir_path, out_path, repair_backend, max_iterations=max_repair_iterations
            )
        except archify_repair.RepairUnavailableError:
            warnings.warn(
                "Archify's optional LLM layout repair was unavailable; "
                "using the deterministic renderer.",
                RuntimeWarning,
                stacklevel=2,
            )
        else:
            if report.get("ok"):
                return out_path
            warnings.warn(
                "Archify's optional LLM layout repair did not resolve the layout; "
                "using the deterministic renderer.",
                RuntimeWarning,
                stacklevel=2,
            )

    last_report: dict | None = None
    for multiplier in _SPACING_RETRY_MULTIPLIERS:
        ir = to_architecture_ir(understanding, title, spacing_multiplier=multiplier, view=view)
        ir_path.write_text(json.dumps(ir.model_dump_archify(), indent=2), encoding="utf-8")
        report = archify_repair.run_deliver_json(bin_cmd, ir_path, out_path)
        if report.get("ok"):
            return out_path
        last_report = report

        # archify's own validator already computed the exact fix for a
        # "layout/constraint" label-overlap failure (a concrete suggested
        # labelAt) - confirmed live (12 Sep 2026) that widening gapY alone
        # doesn't move a fixed-offset default label at all, so apply the
        # suggested fix directly and retry, repeating up to
        # _MAX_LABEL_FIX_ROUNDS times at this same spacing level (fixing one
        # overlap can reveal or shift another on a real dense graph -
        # confirmed live that a single attempt wasn't always enough) before
        # escalating to more room.
        for _ in range(_MAX_LABEL_FIX_ROUNDS):
            patched_ir = _apply_suggested_label_fixes(ir, last_report.get("diagnostics") or [])
            if patched_ir is None:
                break
            ir = patched_ir
            ir_path.write_text(json.dumps(ir.model_dump_archify(), indent=2), encoding="utf-8")
            report = archify_repair.run_deliver_json(bin_cmd, ir_path, out_path)
            if report.get("ok"):
                return out_path
            last_report = report

    raise subprocess.CalledProcessError(
        1,
        [*bin_cmd, "deliver", "architecture", str(ir_path), str(out_path)],
        output=json.dumps(last_report),
    )
