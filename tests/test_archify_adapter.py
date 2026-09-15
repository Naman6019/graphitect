import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from graphitect.deliver.archify_adapter import (
    _GRID_GAP_X,
    _GRID_ORIGIN,
    _aggregate_by_community,
    _apply_suggested_label_fixes,
    _assign_grid,
    _classify_component,
    _estimate_box_size,
    _resolve_archify_bin,
    _route_around_obstacles,
    node_id_remap,
    render,
    render_sequence_trace,
    render_workflow_story,
    to_architecture_ir,
    to_sequence_spec,
    to_workflow_spec,
    workflow_hover_details,
)
from graphitect.deliver.archify_ir import ArchitectureIR, Component, Connection, Meta
from graphitect.models import DesignDocSection, GroundedUnderstanding


def test_classify_component_uses_keyword_heuristics():
    assert _classify_component({"id": "auth_gate", "label": "AuthGate"}) == "security"
    assert _classify_component({"id": "users_table", "label": "Users table"}) == "database"
    assert _classify_component({"id": "hook", "label": "post-commit hook"}) == "external"
    assert _classify_component({"id": "mystery", "label": "does a thing"}) == "backend"


def test_assign_grid_layers_by_dependency_depth():
    nodes = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    edges = [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]
    positions = _assign_grid(nodes, edges, cols=5)
    assert positions["a"][0] < positions["b"][0] < positions["c"][0]


def test_assign_grid_never_collides_when_a_depth_has_more_nodes_than_cols():
    # Regression test for a real live bug: col = row_counts[row] % cols wraps
    # around once a single dependency depth has more than `cols` nodes,
    # silently placing multiple nodes in the exact same (row, col) cell -
    # confirmed live against FundersAI's real graph, where archify's layout
    # validator rejected an overlapping "c0"/"c10" pair both sitting at
    # (row 0, col 0). None of these 12 nodes have any edges, so they all
    # land at depth 0 - more than cols=5 - and must still get distinct cells.
    nodes = [{"id": f"n{i}"} for i in range(12)]
    positions = _assign_grid(nodes, edges=[], cols=5)
    assert len(set(positions.values())) == len(nodes)


def test_to_architecture_ir_rejects_non_architecture_diagrams():
    gu = GroundedUnderstanding(diagram_kind="workflow")
    with pytest.raises(ValueError):
        to_architecture_ir(gu, title="x")


def test_to_architecture_ir_produces_grid_positioned_components():
    gu = GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[{"id": "api", "label": "API"}, {"id": "db", "label": "Database"}],
        edges=[{"from": "api", "to": "db", "label": "SQL"}],
    )
    ir = to_architecture_ir(gu, title="Test System")
    ids = {c.id for c in ir.components}
    assert ids == {"api", "db"}
    assert ir.connections[0].to == "db"
    assert ir.layout.mode == "grid"


def test_render_invokes_the_real_archify_cli_signature(tmp_path: Path):
    # Regression test: archify's actual CLI is
    #   archify deliver <type> <input.json> [output.html]
    # confirmed by running the real CLI directly (12 Sep 2026) - a previous
    # version of this call used a nonexistent `--out` flag and omitted the
    # required <type> argument, so it would have failed even with archify on
    # PATH. Locks in the correct argument order so it can't silently drift again.
    gu = GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[{"id": "api", "label": "API"}],
        edges=[],
    )
    out_path = tmp_path / "diagram.html"

    # render()'s no-repair-backend path shells out via
    # archify_repair.run_deliver_json (--json, so it gets structured
    # diagnostics for the mechanical spacing/label-fix retries) rather than
    # calling subprocess.run directly itself - mock at that level.
    with patch("graphitect.deliver.archify_repair.subprocess.run") as mock_run:
        mock_run.return_value = SimpleNamespace(stdout='{"ok": true}', stderr="", returncode=0)
        render(gu, "Test", out_path, archify_bin="archify")

    args = mock_run.call_args[0][0]
    assert args[0] == "archify"
    assert args[1] == "deliver"
    assert args[2] == "architecture"  # <type>, positional, before the input file
    assert args[3].endswith(".architecture.json")
    assert args[4] == str(out_path)
    assert "--out" not in args  # that flag never existed


def test_render_preserves_windows_paths_in_multi_token_archify_bin(tmp_path: Path):
    # Regression test for a real live bug: shlex.split()'s default POSIX mode
    # treats backslash as an escape character, so a Windows path like
    # "node C:\Users\...\archify.mjs" silently loses every backslash and
    # becomes an unresolvable module path. Must use posix=False.
    gu = GroundedUnderstanding(diagram_kind="architecture", nodes=[{"id": "api"}], edges=[])
    out_path = tmp_path / "diagram.html"
    windows_bin = r"node C:\Users\naman\archify\bin\archify.mjs"

    with patch("graphitect.deliver.archify_repair.subprocess.run") as mock_run:
        mock_run.return_value = SimpleNamespace(stdout='{"ok": true}', stderr="", returncode=0)
        render(gu, "Test", out_path, archify_bin=windows_bin)

    args = mock_run.call_args[0][0]
    assert args[0] == "node"
    assert args[1] == r"C:\Users\naman\archify\bin\archify.mjs"


def _workflow_understanding() -> GroundedUnderstanding:
    return GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[
            {"id": "route", "label": "Route", "source_file": "app/routes.py"},
            {"id": "service", "label": "Ingestion Service", "source_file": "app/service.py"},
            {"id": "parser", "label": "Document Parser", "source_file": "app/parser.py"},
            {"id": "store", "label": "Record Store", "source_file": "app/store.py"},
            {"id": "test_helper", "label": "Test Helper", "source_file": "tests/helper.py"},
        ],
        edges=[
            {"source": "route", "target": "service", "relation": "calls"},
            {"source": "service", "target": "parser", "relation": "uses"},
            {"source": "parser", "target": "store", "relation": "calls"},
            {"source": "service", "target": "test_helper", "relation": "uses"},
        ],
        doc=[DesignDocSection(heading="Key workflows", related_node_ids=["service", "parser"])],
    )


def test_workflow_story_is_a_short_continuous_path_of_direct_graph_edges():
    spec = to_workflow_spec(_workflow_understanding(), "Test")

    assert spec["diagram_type"] == "workflow"
    assert spec["meta"]["animation"] == "trace"
    assert spec["meta"]["visual_preset"] == "signal-flow"
    assert spec["meta"]["quality_profile"] == "showcase"
    assert "viewBox" not in spec["meta"]  # Archify measures exact symbol widths itself.
    assert spec["meta"]["views"] == [
        {
            "id": "traced-path",
            "label": "Traced code path",
            "focus": spec["mainPath"],
            "note": "Follow each direct Graphify relationship in order; no transitive links are added.",
        }
    ]
    assert len(spec["mainPath"]) >= 2
    main_edges = [edge for edge in spec["edges"] if edge["role"] == "main"]
    assert [(edge["from"], edge["to"]) for edge in main_edges] == list(
        zip(spec["mainPath"], spec["mainPath"][1:])
    )
    assert all(node["label"] != "Test Helper" for node in spec["nodes"])


def test_workflow_story_does_not_treat_a_legacy_ollama_symbol_as_provider_topology():
    understanding = _workflow_understanding()
    understanding.nodes[1]["label"] = "function_ollama_chat"

    spec = to_workflow_spec(understanding, "Test")
    details = workflow_hover_details(understanding)

    assert "LLM Provider Adapter" in {node["label"] for node in spec["nodes"]}
    adapter_detail = next(
        detail for detail in details.values() if detail["symbol"] == "function_ollama_chat"
    )
    assert "not evidence that a particular provider is used" in adapter_detail["summary"]


def test_render_workflow_story_uses_archifys_workflow_renderer(tmp_path: Path):
    out_path = tmp_path / "workflow.html"
    with patch("graphitect.deliver.archify_repair.subprocess.run") as mock_run:
        mock_run.return_value = SimpleNamespace(stdout='{"ok": true}', stderr="", returncode=0)
        result = render_workflow_story(
            _workflow_understanding(), "Test", out_path, archify_bin="archify"
        )

    assert result == out_path
    args = mock_run.call_args[0][0]
    assert args[0:3] == ["archify", "deliver", "workflow"]
    spec = json.loads(Path(args[3]).read_text(encoding="utf-8"))
    assert spec["diagram_type"] == "workflow"


def _sequence_understanding() -> GroundedUnderstanding:
    return GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[
            {"id": "route", "label": "Route", "source_file": "app/routes.py"},
            {"id": "handler", "label": "Request Handler", "source_file": "app/handler.py"},
            {"id": "service", "label": "Ingestion Service", "source_file": "app/service.py"},
            {"id": "store", "label": "Record Store", "source_file": "app/store.py"},
            {"id": "test_helper", "label": "Test Helper", "source_file": "tests/helper.py"},
        ],
        edges=[
            {"source": "route", "target": "handler", "relation": "calls"},
            {"source": "handler", "target": "service", "relation": "invokes"},
            {"source": "service", "target": "store", "relation": "calls"},
            {"source": "service", "target": "test_helper", "relation": "calls"},
            {"source": "route", "target": "store", "relation": "uses"},
        ],
        doc=[DesignDocSection(heading="Key workflows", related_node_ids=["handler", "service"])],
    )


def test_sequence_trace_uses_only_consecutive_direct_call_evidence():
    spec = to_sequence_spec(_sequence_understanding(), "Test")

    assert spec["schema_version"] == 1
    assert spec["diagram_type"] == "sequence"
    assert spec["meta"]["animation"] == "trace"
    assert spec["meta"]["column_fit"] == "spread"
    assert spec["meta"]["quality_profile"] == "showcase"
    assert spec["meta"]["subtitle"] == "Direct Graphify calls/invocations; not runtime telemetry."
    assert spec["meta"]["legend"]["entries"]["emphasis"]["label"] == "direct static call"
    assert [message["label"] for message in spec["messages"]] == ["calls", "invokes", "calls"]
    assert [(message["from"], message["to"]) for message in spec["messages"]] == [
        ("step-1", "step-2"),
        ("step-2", "step-3"),
        ("step-3", "step-4"),
    ]
    assert all(participant["label"] != "Test Helper" for participant in spec["participants"])


def test_sequence_trace_is_omitted_without_a_consecutive_call_chain():
    with pytest.raises(ValueError, match="two consecutive direct Graphify calls"):
        to_sequence_spec(_workflow_understanding(), "Test")


def test_sequence_trace_ignores_explicitly_inferred_symbol_links():
    understanding = GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[{"id": "route"}, {"id": "service"}, {"id": "store"}],
        edges=[
            {
                "source": "route",
                "target": "service",
                "relation": "calls",
                "context": "call",
                "confidence": "INFERRED",
            },
            {
                "source": "service",
                "target": "store",
                "relation": "calls",
                "context": "call",
                "confidence": "INFERRED",
            },
        ],
    )

    with pytest.raises(ValueError, match="two consecutive direct Graphify calls"):
        to_sequence_spec(understanding, "Test")


def test_render_sequence_trace_uses_archifys_sequence_renderer(tmp_path: Path):
    out_path = tmp_path / "sequence.html"
    with patch("graphitect.deliver.archify_repair.subprocess.run") as mock_run:
        mock_run.return_value = SimpleNamespace(stdout='{"ok": true}', stderr="", returncode=0)
        result = render_sequence_trace(
            _sequence_understanding(), "Test", out_path, archify_bin="archify"
        )

    assert result == out_path
    args = mock_run.call_args[0][0]
    assert args[0:3] == ["archify", "deliver", "sequence"]
    spec = json.loads(Path(args[3]).read_text(encoding="utf-8"))
    assert spec["diagram_type"] == "sequence"


def _small_ir() -> ArchitectureIR:
    return ArchitectureIR(
        meta=Meta(title="Test"),
        components=[
            Component(id="api_main", type="backend", label="API", row=0, col=0),
            Component(id="api_routes", type="backend", label="Routes", row=1, col=0),
        ],
        connections=[
            Connection(id="api_main-api_routes", **{"from": "api_main"}, to="api_routes", label="imports")
        ],
    )


class TestApplySuggestedLabelFixes:
    """Regression coverage for a real live bug found testing the "no LLM
    key" fallback (12 Sep 2026): even a genuinely small, non-aggregated
    5-node graph failed archify's validator with a label overlapping its
    own FROM component - and confirmed live that widening gapY does
    nothing, since archify's default label placement sits at a fixed
    offset regardless of the actual row gap. archify's own diagnostic
    already computes the exact fix; this just has to apply it.
    """

    _DIAGNOSTIC = {
        "code": "layout/constraint",
        "severity": "error",
        "message": (
            'Label "imports" overlaps component "api_main" - adjust '
            "labelDx/labelDy/labelSegment or set labelAt.\n"
            "  label rect: [78, 120, 44, 14]\n"
            '  component "api_main" rect: [40, 80, 120, 60]\n'
            "  Suggested fix: labelAt [100, 154] or labelDy +24 (below); "
            "or labelAt [100, 76] or labelDy -54 (above)"
        ),
        "subject": {"diagramType": "architecture"},
        "evidence": {},
        "supportedFixes": [],
    }

    def test_applies_the_suggested_labelat_to_the_matching_connection(self):
        patched = _apply_suggested_label_fixes(_small_ir(), [self._DIAGNOSTIC])
        assert patched is not None
        assert patched.connections[0].label_at == (100.0, 154.0)

    def test_leaves_every_other_field_untouched(self):
        original = _small_ir()
        patched = _apply_suggested_label_fixes(original, [self._DIAGNOSTIC])
        assert patched.connections[0].id == original.connections[0].id
        assert patched.connections[0].label == original.connections[0].label
        assert patched.components == original.components

    def test_returns_none_when_no_diagnostic_matches_any_real_connection(self):
        unrelated = {**self._DIAGNOSTIC, "message": self._DIAGNOSTIC["message"].replace("imports", "nonexistent")}
        assert _apply_suggested_label_fixes(_small_ir(), [unrelated]) is None

    def test_ignores_non_layout_constraint_diagnostics(self):
        other = {**self._DIAGNOSTIC, "code": "clean-flow/edge-through-node"}
        assert _apply_suggested_label_fixes(_small_ir(), [other]) is None

    def test_returns_none_for_an_unparseable_message(self):
        garbled = {**self._DIAGNOSTIC, "message": "something failed, no useful details"}
        assert _apply_suggested_label_fixes(_small_ir(), [garbled]) is None


def test_render_applies_mechanical_label_fix_before_escalating_spacing(tmp_path: Path):
    # The combined no-LLM retry strategy: first attempt fails with a
    # parseable layout/constraint diagnostic -> apply archify's own
    # suggested fix and retry at the SAME spacing level, rather than
    # immediately widening gapY (confirmed live: widening gapY alone does
    # nothing for this failure mode).
    gu = GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[{"id": "api_main", "label": "API"}, {"id": "api_routes", "label": "Routes"}],
        edges=[{"from": "api_main", "to": "api_routes", "label": "imports"}],
    )
    out_path = tmp_path / "diagram.html"

    diag = {
        "code": "layout/constraint",
        "message": (
            'Label "imports" overlaps component "api_main" - '
            "Suggested fix: labelAt [100, 154] or labelDy +24 (below)"
        ),
    }
    responses = iter(
        [
            SimpleNamespace(stdout=json.dumps({"ok": False, "diagnostics": [diag]}), stderr="", returncode=1),
            SimpleNamespace(stdout=json.dumps({"ok": True}), stderr="", returncode=0),
        ]
    )

    with patch("graphitect.deliver.archify_repair.subprocess.run", side_effect=lambda *a, **k: next(responses)):
        result = render(gu, "Test", out_path, archify_bin="archify")

    assert result == out_path


def test_render_escalates_spacing_when_mechanical_fix_cannot_help(tmp_path: Path):
    # When no diagnostic can be parsed/matched at all, there's nothing to
    # mechanically patch - render() must still fall back to widening gapY
    # (the only other lever it has without an LLM) rather than giving up
    # after just one failed attempt.
    gu = GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[{"id": "a"}, {"id": "b"}],
        edges=[],
    )
    out_path = tmp_path / "diagram.html"

    unparseable_failure = SimpleNamespace(
        stdout=json.dumps({"ok": False, "diagnostics": [{"code": "layout/constraint", "message": "no details"}]}),
        stderr="",
        returncode=1,
    )
    success = SimpleNamespace(stdout=json.dumps({"ok": True}), stderr="", returncode=0)
    # First spacing attempt fails (no mechanical fix possible - message
    # doesn't parse), second spacing attempt (wider gapY) succeeds.
    responses = iter([unparseable_failure, success])

    with patch("graphitect.deliver.archify_repair.subprocess.run", side_effect=lambda *a, **k: next(responses)):
        result = render(gu, "Test", out_path, archify_bin="archify")

    assert result == out_path


def test_render_raises_after_exhausting_all_spacing_and_label_fix_attempts(tmp_path: Path):
    gu = GroundedUnderstanding(diagram_kind="architecture", nodes=[{"id": "a"}, {"id": "b"}], edges=[])
    out_path = tmp_path / "diagram.html"

    always_fails = SimpleNamespace(
        stdout=json.dumps({"ok": False, "diagnostics": [{"code": "layout/constraint", "message": "no details"}]}),
        stderr="",
        returncode=1,
    )

    with patch("graphitect.deliver.archify_repair.subprocess.run", return_value=always_fails):
        with pytest.raises(subprocess.CalledProcessError):
            render(gu, "Test", out_path, archify_bin="archify")


_ROUTER_BOX_W, _ROUTER_BOX_H, _ROUTER_CELL_W, _ROUTER_GAP_Y, _ROUTER_COLS = 120, 60, 150, 80, 5


def _router_kwargs(**overrides):
    kwargs = dict(
        box_width=_ROUTER_BOX_W,
        box_height=_ROUTER_BOX_H,
        cell_w=_ROUTER_CELL_W,
        cell_h=_ROUTER_BOX_H,
        gap_x=_GRID_GAP_X,
        gap_y=_ROUTER_GAP_Y,
        cols=_ROUTER_COLS,
    )
    kwargs.update(overrides)
    return kwargs


def _component_rect(row: int, col: int) -> tuple[float, float, float, float]:
    ox, oy = _GRID_ORIGIN
    step_x = _ROUTER_CELL_W + _GRID_GAP_X
    step_y = _ROUTER_BOX_H + _ROUTER_GAP_Y
    x = ox + col * step_x
    y = oy + row * step_y
    return x, y, x + _ROUTER_BOX_W, y + _ROUTER_BOX_H


def _segment_hits_rect(p1: tuple[float, float], p2: tuple[float, float], rect: tuple[float, float, float, float]) -> bool:
    """True if the axis-aligned segment p1->p2 passes through rect's
    interior. Router waypoints are always axis-aligned (shared x or shared
    y) by construction, so only those two cases need handling.
    """
    x1, y1 = p1
    x2, y2 = p2
    rx1, ry1, rx2, ry2 = rect
    if y1 == y2:  # horizontal segment
        if not (rx1 < max(x1, x2) and min(x1, x2) < rx2):
            return False
        return ry1 < y1 < ry2
    if x1 == x2:  # vertical segment
        if not (ry1 < max(y1, y2) and min(y1, y2) < ry2):
            return False
        return rx1 < x1 < rx2
    raise AssertionError(f"non-axis-aligned segment {p1}->{p2}")


def _full_path(conn: Connection, from_rect, to_rect) -> list[tuple[float, float]]:
    """Box-edge exit point -> via waypoints -> box-edge entry point, so the
    segments touching each endpoint box get checked too, not just the
    waypoints in open space.
    """
    from_x_mid, to_x_mid = (from_rect[0] + from_rect[2]) / 2, (to_rect[0] + to_rect[2]) / 2
    side_point = {
        "top": lambda r: ((r[0] + r[2]) / 2, r[1]),
        "bottom": lambda r: ((r[0] + r[2]) / 2, r[3]),
        "left": lambda r: (r[0], (r[1] + r[3]) / 2),
        "right": lambda r: (r[2], (r[1] + r[3]) / 2),
    }
    start = side_point[conn.from_side](from_rect) if conn.from_side else (from_x_mid, (from_rect[1] + from_rect[3]) / 2)
    end = side_point[conn.to_side](to_rect) if conn.to_side else (to_x_mid, (to_rect[1] + to_rect[3]) / 2)
    return [start, *(conn.via or []), end]


class TestRouteAroundObstacles:
    """Regression coverage for the real live bug found testing "the graph
    is a must" for large codebases with no LLM key (12 Sep 2026): an
    earlier version of this router connected box centers directly through
    box-body height and was confirmed live (against FundersAI's real
    7,885-node graph) to still cross other components - archify's own
    validator kept rejecting it with the exact same diagnostics. The fix
    routes exclusively through the empty inter-row gap strips and an empty
    side lane, verified here by checking that no routed segment ever
    crosses ANY unrelated component's rectangle, not just by trusting the
    waypoint math looks plausible.
    """

    def test_adjacent_row_and_column_connections_are_left_untouched(self):
        positions = {"a": (0, 0), "b": (1, 0)}
        conn = Connection(id="a-b", **{"from": "a"}, to="b")
        routed = _route_around_obstacles([conn], positions, **_router_kwargs())
        assert routed[0].via is None
        assert routed[0].from_side is None

    def test_leaves_connections_untouched_when_nothing_is_actually_in_the_way(self):
        # Regression test for a real live bug: an earlier, purely
        # distance-based version rerouted every "2+ rows apart" or "2+
        # columns apart, same row" connection regardless of whether
        # anything real sat between them - confirmed live to reroute far
        # more connections than necessary, producing a needlessly busy
        # diagram of long side-lane detours. With nothing else in `positions`
        # to actually block it, a far same-row connection must be left on
        # archify's own default routing.
        positions = {"a": (0, 0), "b": (0, 3)}
        conn = Connection(id="a-b", **{"from": "a"}, to="b")
        routed = _route_around_obstacles([conn], positions, **_router_kwargs())[0]
        assert routed.via is None
        assert routed.from_side is None

    def test_same_row_far_column_dips_into_the_rows_own_gap_strip_when_blocked(self):
        positions = {"a": (0, 0), "b": (0, 3), "blocker": (0, 1)}
        conn = Connection(id="a-b", **{"from": "a"}, to="b")
        routed = _route_around_obstacles([conn], positions, **_router_kwargs())[0]
        assert routed.from_side == routed.to_side == "bottom"
        assert len(routed.via) == 2
        assert routed.via[0][1] == routed.via[1][1]  # both waypoints share the same y (the gap strip)

    def test_multi_row_gap_routes_through_the_side_lane_when_blocked(self):
        positions = {"a": (0, 0), "b": (3, 0), "blocker": (1, 0)}
        conn = Connection(id="a-b", **{"from": "a"}, to="b")
        routed = _route_around_obstacles([conn], positions, **_router_kwargs())[0]
        assert routed.from_side == "bottom"
        assert routed.to_side == "top"
        assert len(routed.via) == 4
        assert routed.via[1][0] == routed.via[2][0]  # the two lane waypoints share the lane's x

    def test_reversed_multi_row_gap_uses_top_bottom_sides(self):
        positions = {"a": (3, 0), "b": (0, 0), "blocker": (1, 0)}  # "from" is BELOW "to" this time
        conn = Connection(id="a-b", **{"from": "a"}, to="b")
        routed = _route_around_obstacles([conn], positions, **_router_kwargs())[0]
        assert routed.from_side == "top"
        assert routed.to_side == "bottom"

    def test_adjacent_rows_with_a_blocker_between_far_columns_still_get_rerouted(self):
        # A row-distance-only heuristic would never catch this: from/to are
        # only ONE row apart, but a third component sits between their
        # columns in the shared row, directly in the way.
        positions = {"a": (1, 2), "b": (0, 0), "blocker": (0, 1)}
        conn = Connection(id="a-b", **{"from": "a"}, to="b")
        routed = _route_around_obstacles([conn], positions, **_router_kwargs())[0]
        assert routed.via is not None
        assert len(routed.via) == 2  # adjacent rows still only need the one shared gap strip, no lane

    def test_no_routed_segment_crosses_an_unrelated_component_in_a_realistic_grid(self):
        # Mirrors the real shape of the bug: several components stacked
        # densely across rows and columns, with long-distance connections
        # skipping over rows and columns full of unrelated boxes.
        positions = {f"n{r}_{c}": (r, c) for r in range(4) for c in range(4)}
        rects = {nid: _component_rect(r, c) for nid, (r, c) in positions.items()}
        connections = [
            Connection(id="c1", **{"from": "n0_0"}, to="n3_0"),  # skips rows 1,2 in its own column
            Connection(id="c2", **{"from": "n0_1"}, to="n3_3"),  # skips rows AND columns
            Connection(id="c3", **{"from": "n2_0"}, to="n2_3"),  # same row, far column
            Connection(id="c4", **{"from": "n1_2"}, to="n0_0"),  # adjacent rows, but n0_1 sits directly between them
        ]
        routed = _route_around_obstacles(connections, positions, **_router_kwargs())
        rerouted = [c for c in routed if c.via is not None]
        assert len(rerouted) == 4  # all four genuinely have something in their way in this dense grid

        for conn in rerouted:
            from_rect, to_rect = rects[conn.from_], rects[conn.to]
            path = _full_path(conn, from_rect, to_rect)
            for nid, rect in rects.items():
                if nid in (conn.from_, conn.to):
                    continue
                for p1, p2 in zip(path, path[1:]):
                    assert not _segment_hits_rect(p1, p2, rect), (
                        f"{conn.id}'s route crosses unrelated component {nid}"
                    )


def test_render_delegates_to_the_repair_loop_when_a_backend_is_given(tmp_path: Path):
    # render() must stay a single, direct `deliver` call (unchanged) when no
    # repair_backend is passed - that path is locked in by the two tests
    # above. When one is passed, it should hand off to archify_repair
    # instead of calling subprocess.run directly itself.
    gu = GroundedUnderstanding(diagram_kind="architecture", nodes=[{"id": "api", "label": "API"}], edges=[])
    out_path = tmp_path / "diagram.html"
    fake_backend = object()  # never actually called - repair_and_deliver is mocked

    with patch(
        "graphitect.deliver.archify_adapter.archify_repair.repair_and_deliver",
        return_value={"ok": True},
    ) as mock_repair:
        result = render(gu, "Test", out_path, archify_bin="archify", repair_backend=fake_backend)

    assert result == out_path
    mock_repair.assert_called_once()
    call_kwargs = mock_repair.call_args
    assert call_kwargs.args[0].meta.title == "Test"  # the built ArchitectureIR
    assert call_kwargs.args[2] == out_path.with_suffix(".architecture.json")
    assert call_kwargs.args[3] == out_path
    assert call_kwargs.args[4] is fake_backend


def test_render_falls_back_when_the_repair_loop_never_converges(tmp_path: Path):
    # The repair backend is an enhancement, never a prerequisite. If it
    # cannot resolve the layout, render() must retry deterministically and
    # still return a diagram rather than terminating graphitect build.
    gu = GroundedUnderstanding(diagram_kind="architecture", nodes=[{"id": "api", "label": "API"}], edges=[])
    out_path = tmp_path / "diagram.html"

    with patch(
        "graphitect.deliver.archify_adapter.archify_repair.repair_and_deliver",
        return_value={"ok": False, "error": "still broken"},
    ), patch(
        "graphitect.deliver.archify_repair.subprocess.run",
        return_value=SimpleNamespace(stdout='{"ok": true}', stderr="", returncode=0),
    ), pytest.warns(RuntimeWarning, match="did not resolve"):
        result = render(gu, "Test", out_path, archify_bin="archify", repair_backend=object())

    assert result == out_path


def test_render_falls_back_when_the_repair_provider_is_unavailable(tmp_path: Path):
    gu = GroundedUnderstanding(diagram_kind="architecture", nodes=[{"id": "api", "label": "API"}], edges=[])
    out_path = tmp_path / "diagram.html"

    from graphitect.deliver.archify_repair import RepairUnavailableError

    with patch(
        "graphitect.deliver.archify_adapter.archify_repair.repair_and_deliver",
        side_effect=RepairUnavailableError("optional LLM layout repair is unavailable"),
    ), patch(
        "graphitect.deliver.archify_repair.subprocess.run",
        return_value=SimpleNamespace(stdout='{"ok": true}', stderr="", returncode=0),
    ), pytest.warns(RuntimeWarning, match="was unavailable"):
        result = render(gu, "Test", out_path, archify_bin="archify", repair_backend=object())

    assert result == out_path


def test_connection_routing_and_label_fields_serialize_to_archifys_real_names():
    # These fields exist purely for archify_repair's LLM loop to set - the
    # deterministic heuristic in this module never sets them - but they must
    # round-trip to archify's exact schema field names (fromSide/toSide/
    # labelAt/labelDx/labelDy/labelSegment), confirmed against
    # archify/schemas/architecture.schema.json's Connection definition, not
    # guessed. And when left unset (the common case), they must not appear
    # in the dumped output at all.
    from graphitect.deliver.archify_ir import Connection

    plain = Connection(id="a-b", **{"from": "a"}, to="b")
    assert plain.model_dump(by_alias=True, exclude_none=True) == {
        "id": "a-b",
        "from": "a",
        "to": "b",
        "variant": "default",
    }

    routed = Connection(
        id="a-b",
        **{"from": "a"},
        to="b",
        fromSide="right",
        toSide="left",
        route="orthogonal-h",
        via=[(10, 20)],
        labelAt=(15, 25),
        labelDx=1,
        labelDy=2,
        labelSegment=0,
    )
    dumped = routed.model_dump(by_alias=True, exclude_none=True)
    assert dumped["fromSide"] == "right"
    assert dumped["toSide"] == "left"
    assert dumped["route"] == "orthogonal-h"
    assert dumped["via"] == [(10, 20)]
    assert dumped["labelAt"] == (15, 25)
    assert dumped["labelDx"] == 1
    assert dumped["labelDy"] == 2
    assert dumped["labelSegment"] == 0


def test_dumped_ir_omits_empty_sources_instead_of_emitting_an_empty_array():
    # Regression test: archify's real schema validator rejects an empty
    # `sources: []` with "must NOT have fewer than 1 items" - the field must
    # be absent entirely, not present-but-empty. Confirmed live against the
    # real `archify validate` (12 Sep 2026).
    gu = GroundedUnderstanding(diagram_kind="architecture", nodes=[{"id": "api", "label": "API"}], edges=[])
    ir = to_architecture_ir(gu, title="Test")
    dumped = ir.model_dump_archify()
    assert "sources" not in dumped["components"][0]


def test_to_architecture_ir_defaults_to_standard_not_showcase_quality():
    # Regression test for a real live bug: "draft" was never a valid archify
    # quality value at all (its real CLI only accepts standard|showcase,
    # confirmed via its own --help), and defaulting to "showcase" made every
    # real multi-node graph fail archify's strict layout validator, since
    # graphitect's own grid layout is a simple heuristic, not hand-tuned for
    # showcase-grade spacing (confirmed live against git-resume-agent's own
    # 12-node graph: label-overlap and connection-crossing failures).
    gu = GroundedUnderstanding(diagram_kind="architecture", nodes=[{"id": "api", "label": "API"}], edges=[])
    ir = to_architecture_ir(gu, title="Test")
    assert ir.meta.quality_profile == "standard"


def test_dumped_ir_omits_empty_top_level_boundaries_and_cards():
    gu = GroundedUnderstanding(diagram_kind="architecture", nodes=[{"id": "api", "label": "API"}], edges=[])
    ir = to_architecture_ir(gu, title="Test")
    dumped = ir.model_dump_archify()
    assert "boundaries" not in dumped
    assert "cards" not in dumped


class TestResolveArchifyBin:
    """Graphitect always uses its bundled Archify runtime."""

    def test_explicit_archify_bin_wins(self):
        assert _resolve_archify_bin("custom-archify", auto_install=True) == ["custom-archify"]

    def test_uses_bundled_archify_with_node(self):
        with patch("graphitect.deliver.archify_adapter.shutil.which", return_value="/usr/bin/node"), patch(
            "graphitect.deliver.archify_adapter._BUNDLED_ARCHIFY_PATH"
        ) as mock_path:
            mock_path.exists.return_value = True
            mock_path.__str__ = lambda self: "/pkg/graphitect/_vendor/archify/bin/archify.mjs"
            result = _resolve_archify_bin(None, auto_install=True)
        assert result[0] == "/usr/bin/node"
        assert "archify.mjs" in result[1]

    def test_raises_when_bundled_runtime_is_missing(self):
        with patch("graphitect.deliver.archify_adapter._BUNDLED_ARCHIFY_PATH") as mock_path:
            mock_path.exists.return_value = False
            with pytest.raises(FileNotFoundError, match="bundled Archify runtime is missing"):
                _resolve_archify_bin(None, auto_install=False)

    def test_raises_when_node_is_unavailable(self):
        with patch("graphitect.deliver.archify_adapter.shutil.which", return_value=None), patch(
            "graphitect.deliver.archify_adapter._BUNDLED_ARCHIFY_PATH"
        ) as mock_path:
            mock_path.exists.return_value = True
            with pytest.raises(FileNotFoundError, match=r"Node.js 18\+"):
                _resolve_archify_bin(None, auto_install=True)


def test_estimate_box_size_widens_for_the_longest_label():
    # Regression test for a real live bug: archify's own grid default (120px)
    # only fits short labels - a real 12-node graph with labels like "Schema
    # Discoverer Agent" (~152px per archify's own measurement) failed
    # archify's layout validator with "label wider than component".
    narrow_w, narrow_h = _estimate_box_size(["API"])
    wide_w, wide_h = _estimate_box_size(["API", "Portfolio Description Agent"])
    assert wide_w > narrow_w
    assert narrow_h == wide_h == 60


def test_estimate_box_size_never_shrinks_below_archifys_own_default():
    w, h = _estimate_box_size(["x"])
    assert w >= 120


class TestAggregateByCommunity:
    """Naman's point (12 Sep 2026): feeding a large codebase's full raw graph
    into the diagram either fails to render or renders unreadably. Mirrors
    Graphify's own community-view fallback for exactly this reason, using
    Graphify's real computed communities rather than an LLM's guess.
    """

    def test_collapses_nodes_sharing_a_community_into_one_box(self):
        nodes = [
            {"id": "a", "label": "A", "community": 1},
            {"id": "b", "label": "B", "community": 1},
            {"id": "c", "label": "C", "community": 2},
        ]
        agg_nodes, _ = _aggregate_by_community(nodes, [], {})
        assert len(agg_nodes) == 2

    def test_uses_real_graphify_community_labels_when_available(self):
        nodes = [{"id": "a", "label": "A", "community": 7}]
        agg_nodes, _ = _aggregate_by_community(nodes, [], {"7": "AMC Discovery Agents"})
        assert agg_nodes[0]["label"] == "AMC Discovery Agents"

    def test_falls_back_to_a_generic_label_without_real_community_labels(self):
        nodes = [{"id": "a", "label": "A", "community": 7}]
        agg_nodes, _ = _aggregate_by_community(nodes, [], {})
        assert agg_nodes[0]["label"]  # non-empty, doesn't crash without labels

    def test_derives_semantic_names_from_dominant_source_files_and_symbols(self):
        nodes = [
            {
                "id": "frontend_api",
                "label": "POST()",
                "community": 1,
                "source_file": "career-agent/frontend/src/app/api/jobs/route.ts",
            },
            {
                "id": "careers_graph",
                "label": "TalentOS // Careers pipeline",
                "community": 2,
                "source_file": "career-agent/career_agent/graph.py",
            },
            {
                "id": "studio_graph",
                "label": "FreelanceGraphState",
                "community": 3,
                "source_file": "career-agent/career_agent/freelance_graph.py",
            },
            {
                "id": "source",
                "label": "portal_source()",
                "community": 4,
                "source_file": "career-agent/career_agent/sources/company_portals.py",
            },
            {
                "id": "store",
                "label": "save_application()",
                "community": 5,
                "source_file": "career-agent/career_agent/storage/firestore_store.py",
            },
            {
                "id": "notify",
                "label": "send_digest()",
                "community": 6,
                "source_file": "career-agent/career_agent/tools/notify.py",
            },
        ]

        agg_nodes, _ = _aggregate_by_community(nodes, [], {})
        labels = {node["label"] for node in agg_nodes}

        assert labels == {
            "Frontend API",
            "Careers Pipeline",
            "Studio Pipeline",
            "Company Sources",
            "Firestore Records",
            "Notifications",
        }

    def test_ignores_placeholder_graphify_labels_when_repository_evidence_is_better(self):
        nodes = [
            {
                "id": "matching",
                "label": "title_matches()",
                "community": 7,
                "source_file": "career-agent/career_agent/matching.py",
            }
        ]
        agg_nodes, _ = _aggregate_by_community(nodes, [], {"7": "Community 7"})
        assert agg_nodes[0]["label"] == "Job Matching"

    def test_frontend_imports_do_not_override_the_dominant_file_role(self):
        nodes = [
            {
                "id": "velocity",
                "label": "AuthContext",
                "community": 7,
                "source_file": "career-agent/frontend/src/components/ui/scroll-based-velocity.tsx",
            }
        ]
        agg_nodes, _ = _aggregate_by_community(nodes, [], {})
        assert agg_nodes[0]["label"] == "Frontend UI"

    def test_distinguishes_firestore_records_from_budget_and_run_controls(self):
        nodes = [
            {
                "id": "records",
                "label": "save_application()",
                "community": 1,
                "source_file": "career-agent/career_agent/storage/firestore_store.py",
            },
            {
                "id": "budget",
                "label": "reserve_llm_budget()",
                "community": 2,
                "source_file": "career-agent/career_agent/storage/firestore_store.py",
            },
            {
                "id": "settle",
                "label": "settle_llm_budget()",
                "community": 2,
                "source_file": "career-agent/career_agent/storage/firestore_store.py",
            },
        ]
        agg_nodes, _ = _aggregate_by_community(nodes, [], {})
        assert {node["label"] for node in agg_nodes} == {
            "Firestore Records",
            "Firestore Budgets & Runs",
        }

    def test_adds_a_source_grounded_suffix_when_generic_labels_collide(self):
        nodes = [
            {
                "id": "button",
                "label": "Button()",
                "community": 1,
                "source_file": "frontend/src/components/ui/button.tsx",
            },
            {
                "id": "modal",
                "label": "Modal()",
                "community": 2,
                "source_file": "frontend/src/components/ui/modal.tsx",
            },
        ]
        agg_nodes, _ = _aggregate_by_community(nodes, [], {})
        assert {node["label"] for node in agg_nodes} == {
            "Frontend UI (Button)",
            "Frontend UI (Modal)",
        }

    def test_minor_source_imports_do_not_override_the_dominant_file_role(self):
        nodes = [
            {
                "id": "progress",
                "label": "report_progress()",
                "community": 1,
                "source_file": "career-agent/career_agent/run_progress.py",
            },
            {
                "id": "progress_state",
                "label": "ProgressState",
                "community": 1,
                "source_file": "career-agent/career_agent/run_progress.py",
            },
            {
                "id": "ats",
                "label": "fetch_ats()",
                "community": 1,
                "source_file": "career-agent/career_agent/sources/ats_boards.py",
            },
        ]
        agg_nodes, _ = _aggregate_by_community(nodes, [], {})
        assert agg_nodes[0]["label"] == "Run Progress"

    def test_test_only_communities_do_not_displace_production_components(self):
        production = [
            {
                "id": f"prod{i}",
                "label": f"Module {i}",
                "community": i,
                "source_file": f"src/module_{i}.py",
            }
            for i in range(15)
        ]
        tests = [
            {
                "id": f"test{i}",
                "label": f"test_case_{i}",
                "community": "tests",
                "source_file": "tests/test_everything.py",
            }
            for i in range(100)
        ]

        agg_nodes, _ = _aggregate_by_community([*production, *tests], [], {})
        assert "ctests" not in {node["id"] for node in agg_nodes}

    def test_drops_intra_community_edges_keeps_cross_community_edges(self):
        nodes = [
            {"id": "a", "label": "A", "community": 1},
            {"id": "b", "label": "B", "community": 1},
            {"id": "c", "label": "C", "community": 2},
        ]
        edges = [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]
        agg_nodes, agg_edges = _aggregate_by_community(nodes, edges, {})
        assert len(agg_edges) == 1  # a->b is intra-community, dropped
        assert {agg_edges[0]["from"], agg_edges[0]["to"]} == {n["id"] for n in agg_nodes}

    def test_deduplicates_multiple_cross_community_edges_into_one_with_a_count(self):
        nodes = [
            {"id": "a", "label": "A", "community": 1},
            {"id": "b", "label": "B", "community": 2},
            {"id": "c", "label": "C", "community": 2},
        ]
        edges = [{"from": "a", "to": "b"}, {"from": "a", "to": "c"}]
        _, agg_edges = _aggregate_by_community(nodes, edges, {})
        assert len(agg_edges) == 1
        assert "2 connections" in agg_edges[0]["label"]

    def test_nodes_without_a_community_field_each_stay_their_own_box(self):
        nodes = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
        agg_nodes, _ = _aggregate_by_community(nodes, [], {})
        assert len(agg_nodes) == 2

    def test_caps_total_boxes_even_when_there_are_too_many_communities(self):
        # Regression test for a real live bug found against FundersAI's
        # actual graph.json: 548 real communities, not the 2-3 a small
        # unit-test graph has. One box per community isn't sufficient on its
        # own - there must be a second-level cap.
        nodes = [{"id": f"n{i}", "label": f"Node {i}", "community": i} for i in range(50)]
        agg_nodes, _ = _aggregate_by_community(nodes, [], {})
        assert len(agg_nodes) <= 15

    def test_keeps_the_largest_communities_and_folds_the_rest_into_other(self):
        # 1 large community (100 members) plus 20 single-member ones = 21
        # distinct communities, over the 15-box cap. The large one must
        # survive; with 14 real-community slots and 1 taken by the large
        # one, 13 of the 20 tiny ones also get to keep their own box (there's
        # room) - only the remaining 7 should fold into "Other". This
        # matches confirmed-live behavior against FundersAI's real 548-
        # community graph: fill every available slot with real communities
        # first, only overflow the true remainder into one bucket.
        nodes = (
            [{"id": f"big{i}", "label": "Big", "community": "big"} for i in range(100)]
            + [{"id": f"small{i}", "label": "Small", "community": f"small{i}"} for i in range(20)]
        )
        agg_nodes, _ = _aggregate_by_community(nodes, [], {})
        ids = {n["id"] for n in agg_nodes}
        assert "cbig" in ids  # the one large community survives
        assert "other_components" in ids
        assert len(agg_nodes) <= 15

        other = next(n for n in agg_nodes if n["id"] == "other_components")
        overflowed = int(other["label"].split("(")[1].rstrip(")"))
        kept_small_communities = len(agg_nodes) - 2  # total minus "big" minus "other_components"
        assert overflowed == 20 - kept_small_communities  # every node accounted for exactly once


class TestNodeIdRemap:
    """Regression coverage for a known, previously-unaddressed gap: a doc
    section's related_node_ids cite real raw Graphify node ids, but once
    aggregation collapses those nodes into community boxes for the diagram
    (TestAggregateByCommunity above), those raw ids no longer exist as SVG
    element ids at all - a node-ref pill's hover-highlight silently matched
    nothing. node_id_remap() gives doc_compiler the exact mapping needed to
    keep pointing at whatever id a node actually renders as.
    """

    def test_identity_when_no_aggregation_applies(self):
        gu = GroundedUnderstanding(
            diagram_kind="architecture",
            nodes=[{"id": "api", "label": "API"}, {"id": "db", "label": "Database"}],
            edges=[],
        )
        assert node_id_remap(gu) == {"api": "api", "db": "db"}

    def test_maps_raw_ids_to_their_community_box_id_when_aggregated(self):
        nodes = [{"id": f"n{i}", "label": f"Node {i}", "community": i % 3} for i in range(20)]
        gu = GroundedUnderstanding(diagram_kind="architecture", nodes=nodes, edges=[])
        remap = node_id_remap(gu)
        # Every raw node id maps to a real diagram component id (not itself,
        # since aggregation kicked in for 20 nodes with only 3 communities).
        assert remap["n0"] == "c0"
        assert remap["n1"] == "c1"
        assert len(set(remap.values())) < len(nodes)

    def test_matches_the_exact_mapping_to_architecture_ir_actually_used(self):
        # The whole point: this must be the SAME mapping the real rendered
        # diagram uses, not a plausible-looking approximation - otherwise a
        # pill would "work" in this test while still pointing at a
        # nonexistent id in the real SVG.
        nodes = (
            [{"id": f"big{i}", "label": "Big", "community": "big"} for i in range(100)]
            + [{"id": f"small{i}", "label": "Small", "community": f"small{i}"} for i in range(20)]
        )
        gu = GroundedUnderstanding(diagram_kind="architecture", nodes=nodes, edges=[])
        remap = node_id_remap(gu)
        ir = to_architecture_ir(gu, title="Test")
        real_component_ids = {c.id for c in ir.components}
        assert set(remap.values()) == real_component_ids


def test_to_architecture_ir_aggregates_when_over_the_component_limit():
    nodes = [{"id": f"n{i}", "label": f"Node {i}", "community": i % 3} for i in range(20)]
    gu = GroundedUnderstanding(diagram_kind="architecture", nodes=nodes, edges=[])
    ir = to_architecture_ir(gu, title="Big Graph")
    assert len(ir.components) <= 3  # collapsed to communities (0, 1, 2), not 20 raw boxes


def test_to_architecture_ir_does_not_aggregate_small_graphs():
    nodes = [{"id": "a", "label": "A", "community": 1}, {"id": "b", "label": "B", "community": 2}]
    gu = GroundedUnderstanding(diagram_kind="architecture", nodes=nodes, edges=[])
    ir = to_architecture_ir(gu, title="Small Graph")
    assert {c.id for c in ir.components} == {"a", "b"}  # real nodes, not community boxes


def test_to_architecture_ir_widens_grid_spacing_to_match_box_size():
    gu = GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[{"id": "a", "label": "A Genuinely Long Component Label"}, {"id": "b", "label": "B"}],
        edges=[{"from": "a", "to": "b", "label": "calls"}],
    )
    ir = to_architecture_ir(gu, title="Test")
    assert ir.layout.cell_w is not None and ir.layout.cell_w > 130  # wider than archify's own default
    assert ir.layout.gap_y is not None and ir.layout.gap_y >= 60
    assert all(c.size is not None for c in ir.components)


def test_overview_keeps_a_compact_subset_but_full_view_keeps_every_connection():
    # A report should open on a readable structural map without pretending
    # that lower-volume relationships do not exist. The complete set belongs
    # to the opt-in Full graph tab.
    nodes = [{"id": f"n{i}", "label": f"Module {i}"} for i in range(8)]
    edges = [
        {"from": f"n{i}", "to": f"n{j}", "label": f"{i + j + 1} connections"}
        for i in range(8)
        for j in range(i + 1, 8)
    ]
    gu = GroundedUnderstanding(diagram_kind="architecture", nodes=nodes, edges=edges)

    overview = to_architecture_ir(gu, title="Test", view="overview")
    full = to_architecture_ir(gu, title="Test", view="full")

    assert len(full.connections) == len(edges)
    assert len(overview.connections) == 18
    assert {component.id for component in overview.components} == {
        component.id for component in full.components
    }
    assert {
        component.id: (component.row, component.col) for component in overview.components
    } == {component.id: (component.row, component.col) for component in full.components}
    represented = {endpoint for connection in overview.connections for endpoint in (connection.from_, connection.to)}
    assert represented == {node["id"] for node in nodes}


def test_story_enables_archify_guidance_from_cited_components():
    gu = GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[{"id": "api", "label": "API"}, {"id": "db", "label": "Database"}],
        edges=[{"from": "api", "to": "db"}],
        doc=[DesignDocSection(heading="Data path", related_node_ids=["api", "db"])],
    )

    story = to_architecture_ir(gu, title="Test", view="story")

    assert story.meta.animation == "trace"
    assert story.meta.visual_preset == "signal-flow"
    assert story.meta.views[0].id == "data-path"
    assert story.meta.views[0].focus == ["api", "db"]
    assert story.meta.views[0].note == "Follows directly observed relationships cited in the Data path explanation."
    assert len(story.connections) == len(to_architecture_ir(gu, title="Test", view="overview").connections)


def test_story_falls_back_to_observed_component_relationships_without_citations():
    gu = GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[{"id": "api", "label": "API"}, {"id": "db", "label": "Database"}],
        edges=[{"from": "api", "to": "db"}],
    )

    story = to_architecture_ir(gu, title="Test", view="story")

    assert story.meta.views
    assert set(story.meta.views[0].focus) == {"api", "db"}
    assert "directly observed" in (story.meta.views[0].note or "")


def test_story_focus_is_a_connected_walk_not_a_grouped_hub():
    gu = GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[
            {"id": "browser", "label": "Browser"},
            {"id": "api", "label": "API"},
            {"id": "cache", "label": "Cache"},
            {"id": "database", "label": "Database"},
        ],
        edges=[
            {"from": "browser", "to": "api"},
            {"from": "api", "to": "cache"},
            {"from": "cache", "to": "database"},
        ],
        doc=[
            DesignDocSection(
                heading="Request path",
                related_node_ids=["browser", "api", "cache", "database"],
            )
        ],
    )

    story = to_architecture_ir(gu, title="Test", view="story")
    focus = story.meta.views[0].focus
    observed_pairs = {frozenset((connection.from_, connection.to)) for connection in story.connections}

    assert len(focus) == 4
    assert all(frozenset(hop) in observed_pairs for hop in zip(focus, focus[1:]))


def test_story_does_not_use_the_aggregation_catch_all_as_its_first_anchor():
    gu = GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[
            *[{"id": f"n{i}", "label": f"Node {i}", "community": i} for i in range(16)],
            {"id": "extra", "label": "Extra", "community": 99},
        ],
        edges=[{"from": "n0", "to": f"n{i}"} for i in range(1, 16)],
    )

    story = to_architecture_ir(gu, title="Test", view="story")

    assert story.meta.views[0].focus[0] != "other_components"
    assert len(story.meta.views[0].focus) <= 5


def test_authored_viewbox_contains_the_side_lane_and_its_routes():
    # Regression coverage for the actual TalentOS clipping bug: Archify's
    # automatic viewBox follows component boxes only, while this edge must
    # take a valid lane outside those boxes to avoid crossing the middle one.
    gu = GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}],
        edges=[{"from": "a", "to": "b"}, {"from": "b", "to": "c"}, {"from": "a", "to": "c"}],
    )
    ir = to_architecture_ir(gu, title="Test", view="full")

    route_points = [point for connection in ir.connections for point in (connection.via or [])]
    assert route_points
    assert ir.meta.view_box is not None
    assert max(point[0] for point in route_points) < ir.meta.view_box[0]
    assert max(point[1] for point in route_points) < ir.meta.view_box[1]
    assert tuple(ir.model_dump_archify()["meta"]["viewBox"]) == ir.meta.view_box


def test_to_architecture_ir_rejects_unknown_view():
    gu = GroundedUnderstanding(diagram_kind="architecture", nodes=[{"id": "a", "label": "A"}], edges=[])
    with pytest.raises(ValueError, match="view must be"):
        to_architecture_ir(gu, title="Test", view="detail")  # type: ignore[arg-type]
