from __future__ import annotations

import pytest

from codex_alias import CodexAlias, InvalidNameError
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
