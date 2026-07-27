from __future__ import annotations

import pytest

from codex_alias import CodexAlias, Config, InvalidNameError
from codex_alias.models import HomeKind


def test_add_profile_creates_home_and_wrapper(mgr: CodexAlias) -> None:
    target = mgr.add_profile("work")
    assert target == mgr.config.bin_dir / "codex-work"
    assert target.is_file()
    assert (mgr.config.profile_root / "work").is_dir()

    script = target.read_text()
    assert 'export CODEX_HOME="${PROFILE_ROOT}/work"' in script
    assert target.stat().st_mode & 0o111  # executable


def test_add_profile_custom_command_name(mgr: CodexAlias) -> None:
    target = mgr.add_profile("side", "codex-sp")
    assert target.name == "codex-sp"


@pytest.mark.parametrize("bad", ["", "has space", "../evil", "a/b"])
def test_invalid_names_rejected(mgr: CodexAlias, bad: str) -> None:
    with pytest.raises(InvalidNameError):
        mgr.add_profile(bad)


def test_list_profiles_reports_sharing(mgr: CodexAlias) -> None:
    mgr.add_profile("work")
    mgr.add_profile("play")
    profiles = mgr.list_profiles()
    assert [p.name for p in profiles] == ["play", "work"]
    assert all(not p.sessions_shared for p in profiles)


def test_remove_wrapper(mgr: CodexAlias) -> None:
    mgr.add_profile("work")
    target, removed = mgr.remove_wrapper("work")
    assert removed is True
    assert not target.exists()
    # profile data survives wrapper removal
    assert (mgr.config.profile_root / "work").is_dir()
    _, removed_again = mgr.remove_wrapper("work")
    assert removed_again is False


def test_run_argv_sets_isolated_home(mgr: CodexAlias) -> None:
    argv, env = mgr.run_argv("work", ["--", "--help"])
    assert argv == ["codex", "--", "--help"]
    assert env["CODEX_HOME"] == str(mgr.config.profile_root / "work")


def test_resume_argv_uses_configured_wrapper(mgr: CodexAlias) -> None:
    home = mgr.config.profile_root / "work"
    argv, env = mgr.resume_argv(home, "session-id")
    assert argv == ["codex", "resume", "session-id"]
    assert env["CODEX_HOME"] == str(home)


def test_generated_wrapper_prefers_runtime_codex_wrapper(mgr: CodexAlias) -> None:
    target = mgr.add_profile("work")
    script = target.read_text()
    assert "CODEXSWITCH_CODEX_WRAPPER" in script
    assert "CODEXSWITCH_CODEX_CMD" in script


def test_config_prefers_codex_wrapper(tmp_path) -> None:
    config = Config.from_env(
        {
            "HOME": str(tmp_path),
            "CODEXSWITCH_CODEX_CMD": "real-codex",
            "CODEXSWITCH_CODEX_WRAPPER": "/tools/codex-wrapper",
        }
    )
    assert config.codex_cmd == "real-codex"
    assert config.codex_wrapper == "/tools/codex-wrapper"
    assert config.effective_codex_cmd == "/tools/codex-wrapper"


def test_resolve_home_ref_kinds(mgr: CodexAlias, monkeypatch) -> None:
    mgr.config.source_home.mkdir(parents=True)
    mgr.add_profile("work")

    # Point CODEX_HOME at a distinct dir so current != source and each kind is
    # classified independently.
    current = mgr.config.profile_root.parent / "current"
    current.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(current))

    assert mgr.resolve_home_ref("@source").kind is HomeKind.SOURCE
    assert mgr.resolve_home_ref("work").kind is HomeKind.PROFILE
    assert mgr.resolve_home_ref("@current").kind is HomeKind.CURRENT


def test_source_equals_current_prefers_current(mgr: CodexAlias) -> None:
    # With CODEX_HOME unset, current home resolves to source_home; CURRENT wins.
    mgr.config.source_home.mkdir(parents=True)
    assert mgr.resolve_home_ref("@source").kind is HomeKind.CURRENT
