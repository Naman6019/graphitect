import json
import os
import re
import textwrap
from pathlib import Path

from graphitect.ground.graphify_source import ground


def _write_graph_json(repo_path: Path, nodes: list[dict], edges: list[dict]) -> None:
    out_dir = repo_path / "graphify-out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "graph.json").write_text(
        json.dumps({"nodes": nodes, "links": edges}), encoding="utf-8"
    )


def test_ground_reads_an_existing_graph_json_without_needing_graphify_installed(tmp_path: Path):
    _write_graph_json(tmp_path, [{"id": "a", "label": "A"}], [{"source": "a", "target": "a"}])
    result = ground(tmp_path)
    assert result["nodes"] == [{"id": "a", "label": "A"}]
    assert result["edges"] == [{"source": "a", "target": "a"}]


def test_ground_groups_communities_by_node(tmp_path: Path):
    _write_graph_json(
        tmp_path,
        [
            {"id": "a", "label": "A", "community": 1},
            {"id": "b", "label": "B", "community": 1},
            {"id": "c", "label": "C", "community": 2},
        ],
        [],
    )
    result = ground(tmp_path)
    assert set(result["communities"][1]) == {"a", "b"}
    assert result["communities"][2] == ["c"]


def test_ground_reads_real_community_labels_when_present(tmp_path: Path):
    _write_graph_json(tmp_path, [{"id": "a", "label": "A", "community": 7}], [])
    (tmp_path / "graphify-out" / ".graphify_labels.json").write_text(
        json.dumps({"7": "AMC Discovery Agents"}), encoding="utf-8"
    )
    result = ground(tmp_path)
    assert result["community_labels"] == {"7": "AMC Discovery Agents"}


def test_ground_returns_empty_labels_when_no_labels_file_exists(tmp_path: Path):
    _write_graph_json(tmp_path, [{"id": "a", "label": "A"}], [])
    result = ground(tmp_path)
    assert result["community_labels"] == {}


def test_ground_raises_a_clear_error_when_no_graph_and_no_graphify_binary(tmp_path: Path):
    import pytest
    from unittest.mock import patch

    with patch("graphitect.ground.graphify_source.importlib.util.find_spec", return_value=None), patch(
        "graphitect.ground.graphify_source.shutil.which", return_value=None
    ):
        with pytest.raises(FileNotFoundError, match="bundled Graphify runtime is missing"):
            ground(tmp_path)


def test_ground_runs_the_bundled_graphify_module(tmp_path: Path):
    from unittest.mock import patch

    def fake_run(cmd, **kwargs):
        assert cmd[1:3] == ["-m", "graphify"]
        _write_graph_json(tmp_path, [{"id": "a", "label": "A"}], [])

    with patch("graphitect.ground.graphify_source.importlib.util.find_spec", return_value=object()), patch(
        "graphitect.ground.graphify_source.subprocess.run", side_effect=fake_run
    ):
        result = ground(tmp_path)

    assert result["nodes"][0]["id"] == "a"


def test_ground_can_keep_graphify_artifacts_in_a_transient_output_dir(tmp_path: Path):
    from unittest.mock import patch

    repo = tmp_path / "repo"
    repo.mkdir()
    transient = tmp_path / "transient-graph"

    def fake_run(cmd, **kwargs):
        assert kwargs["env"]["GRAPHIFY_OUT"] == str(transient.resolve())
        transient.mkdir()
        (transient / "graph.json").write_text(
            json.dumps({"nodes": [{"id": "a", "label": "A"}], "links": []}),
            encoding="utf-8",
        )

    with patch("graphitect.ground.graphify_source.importlib.util.find_spec", return_value=object()), patch(
        "graphitect.ground.graphify_source.subprocess.run", side_effect=fake_run
    ):
        result = ground(repo, output_dir=transient)

    assert result["nodes"][0]["id"] == "a"
    assert not (repo / "graphify-out").exists()


def test_bundled_graphify_hooks_reject_an_out_of_repo_saved_root(tmp_path: Path, monkeypatch):
    # The generated hook reads .graphify_root from a committed directory. It
    # must never let that marker redirect reads or writes outside the repo.
    from graphify.hooks import _REBUILD_BODY_CHECKOUT, _REBUILD_BODY_COMMIT

    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    (repo / "graphify-out").mkdir(parents=True)
    outside.mkdir()
    (repo / "graphify-out" / ".graphify_root").write_text(str(outside), encoding="utf-8")
    monkeypatch.chdir(repo)

    for body in (_REBUILD_BODY_COMMIT, _REBUILD_BODY_CHECKOUT):
        match = re.search(r"(    _root = Path\('\.'\).*?)\n    _rebuild_code\(", body, re.DOTALL)
        assert match, "generated hook root resolution is missing"
        namespace = {"Path": Path, "os": os}
        exec(compile(textwrap.dedent(match.group(1)), "<hook-root>", "exec"), namespace)  # noqa: S102
        assert namespace["_root"].resolve() == repo.resolve()
