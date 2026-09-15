"""Grounding via Graphify's structure graph - most accurate, cheapest per
file, works offline (plan.md §02). Reuses an existing graphify-out/graph.json
if present; otherwise shells out to run graphify first.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def ground(
    repo_path: Path,
    *,
    graphify_bin: str | None = None,
    force_rebuild: bool = False,
    output_dir: Path | None = None,
) -> dict:
    """Return {"nodes": [...], "edges": [...], "communities": {...}} from
    graphify's own graph.json export, running graphify first if needed.

    Never overwrites an existing graphify-out/ blindly - always prefers
    `graphify <path> --update` over a fresh build when a graph already
    exists, learned the hard way running Phase 1 against a live repo
    that already had its own Graphify setup (plan.md §06, Phase 1 risk).
    """
    graph_output = output_dir.resolve() if output_dir else repo_path / "graphify-out"
    graph_json = graph_output / "graph.json"
    bundled_spec = None
    if graphify_bin:
        bin_cmd = [graphify_bin]
    elif (bundled_spec := importlib.util.find_spec("graphify")) is not None:
        # Graphify ships inside the Graphitect wheel. Use the current Python
        # interpreter so no separate `graphify` installation or PATH entry is
        # required.
        bin_cmd = [sys.executable, "-m", "graphify"]
    else:
        on_path = shutil.which("graphify")
        bin_cmd = [on_path] if on_path else None

    if not graph_json.exists() or force_rebuild:
        if not bin_cmd:
            raise FileNotFoundError(
                "bundled Graphify runtime is missing and no existing "
                "graphify-out/graph.json can be reused"
            )
        # Graphitect uses Graphify for deterministic source structure. README
        # and docs are read by Graphitect's own optional LLM pass, so Graphify
        # must never require a second API key just to build the mandatory
        # no-LLM diagram.
        cmd = [*bin_cmd, str(repo_path), "--code-only"]
        if graph_json.exists():
            cmd.append("--update")  # incremental, safe - never a blind full rebuild
        env = os.environ.copy()
        env["GRAPHIFY_EMBEDDED"] = "1"
        if bundled_spec is not None and getattr(bundled_spec, "origin", None):
            # Editable/source installs may only expose Graphify because the
            # checkout is the current directory. The analyzer runs with the
            # target repository as cwd, so preserve the discovered package
            # root for that child process. A normal wheel install already has
            # it on sys.path; this is harmless there.
            package_root = str(Path(bundled_spec.origin).resolve().parent.parent)
            existing_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                package_root + os.pathsep + existing_pythonpath
                if existing_pythonpath
                else package_root
            )
        if output_dir:
            # The product-level `build` command uses a temporary Graphify
            # directory so the report really is its only persistent output.
            env["GRAPHIFY_OUT"] = str(graph_output)
        subprocess.run(cmd, check=True, cwd=repo_path, env=env)

    data = json.loads(graph_json.read_text(encoding="utf-8"))
    return {
        "nodes": data.get("nodes", []),
        "edges": data.get("links", data.get("edges", [])),  # graph.json uses networkx "links"
        "communities": _labels_by_community(data),
        # Plain-language community names, if a real graphify run produced
        # them (graphify-out/.graphify_labels.json). Used to name aggregated
        # diagram boxes ("MF Catalog Review Triage") instead of a bare
        # "Community 42" when a large graph gets collapsed to community
        # level - see archify_adapter._aggregate_by_community.
        "community_labels": _read_community_labels(graph_output),
    }


def _labels_by_community(graph_json_data: dict) -> dict[int, list[str]]:
    by_community: dict[int, list[str]] = {}
    for node in graph_json_data.get("nodes", []):
        cid = node.get("community")
        if cid is not None:
            by_community.setdefault(cid, []).append(node["id"])
    return by_community


def _read_community_labels(graph_output: Path) -> dict[str, str]:
    labels_path = graph_output / ".graphify_labels.json"
    if not labels_path.exists():
        return {}
    return json.loads(labels_path.read_text(encoding="utf-8"))
