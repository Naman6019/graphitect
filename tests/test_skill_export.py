from pathlib import Path

from graphitect.cli import build_parser


def _canonical_skill() -> Path:
    return Path(__file__).parents[1] / "graphitect" / "skill" / "SKILL.md"


def test_repo_skill_wrappers_match_the_packaged_portable_skill():
    root = Path(__file__).parents[1]
    canonical = _canonical_skill().read_text(encoding="utf-8")

    assert (root / ".agents" / "skills" / "graphitect" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == canonical
    assert (root / ".claude" / "skills" / "graphitect" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == canonical


def test_skill_export_writes_the_standard_bundle_to_an_explicit_destination(tmp_path: Path):
    destination = tmp_path / "other-agent" / "graphitect"
    args = build_parser().parse_args(["skill", "export", "--output", str(destination)])

    assert args.func(args) == 0
    assert (destination / "SKILL.md").read_text(encoding="utf-8") == _canonical_skill().read_text(
        encoding="utf-8"
    )
    assert (destination / "agents" / "openai.yaml").is_file()


def test_skill_export_uses_the_right_project_discovery_path_for_each_host(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    parser = build_parser()

    codex_args = parser.parse_args(["skill", "export", "--host", "codex"])
    assert codex_args.func(codex_args) == 0
    assert (tmp_path / ".agents" / "skills" / "graphitect" / "SKILL.md").is_file()

    claude_args = parser.parse_args(["skill", "export", "--host", "claude-code"])
    assert claude_args.func(claude_args) == 0
    assert (tmp_path / ".claude" / "skills" / "graphitect" / "SKILL.md").is_file()


def test_skill_export_refuses_to_overwrite_without_force(tmp_path: Path):
    destination = tmp_path / "graphitect"
    destination.mkdir()
    args = build_parser().parse_args(["skill", "export", "--output", str(destination)])

    assert args.func(args) == 2


def test_skill_export_global_flag(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    parser = build_parser()

    claude_args = parser.parse_args(["skill", "export", "--host", "claude-code", "--global"])
    assert claude_args.func(claude_args) == 0
    assert (tmp_path / ".claude" / "skills" / "graphitect" / "SKILL.md").is_file()

    codex_args = parser.parse_args(["skill", "export", "--host", "codex", "--global"])
    assert codex_args.func(codex_args) == 0
    assert (tmp_path / ".agents" / "skills" / "graphitect" / "SKILL.md").is_file()

