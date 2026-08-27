from __future__ import annotations

import pytest

from codex_alias import (
    CodexAlias,
    CodexAliasError,
    Config,
    InvalidNameError,
    ProfileNotFoundError,
)
from codex_alias.models import HomeKind


def test_add_profile_creates_home_and_wrapper(mgr: CodexAlias) -> None:
    target = mgr.add_profile("work")
    assert target == mgr.config.bin_dir / "codex-work"
    assert target.is_file()
    assert (mgr.config.profile_root / "work").is_dir()

    script = target.read_text()
    assert 'exec "${CODEXALIAS_MANAGER_BIN_NAME:-codexalias}" run work "$@"' in script
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


def test_remove_profile_deletes_wrapper_and_home(mgr: CodexAlias) -> None:
    mgr.add_profile("work")
    mgr.add_profile("play")

    result = mgr.remove_profile("work")

    assert result.wrapper_removed is True
    assert result.home_removed is True
    assert not (mgr.config.bin_dir / "codex-work").exists()
    assert not (mgr.config.profile_root / "work").exists()
    assert [p.name for p in mgr.list_profiles()] == ["play"]


def test_remove_profile_keep_data_keeps_home(mgr: CodexAlias) -> None:
    mgr.add_profile("work")

    result = mgr.remove_profile("work", keep_data=True)

    assert result.wrapper_removed is True
    assert result.home_removed is False
    assert not (mgr.config.bin_dir / "codex-work").exists()
    assert (mgr.config.profile_root / "work").is_dir()


def test_remove_profile_without_wrapper_still_removes_home(mgr: CodexAlias) -> None:
    mgr.add_profile("work")
    (mgr.config.bin_dir / "codex-work").unlink()

    result = mgr.remove_profile("work")

    assert result.wrapper_removed is False
    assert result.home_removed is True
    assert not (mgr.config.profile_root / "work").exists()


def test_remove_profile_removes_custom_command(mgr: CodexAlias) -> None:
    mgr.add_profile("work", "codex-w")
    (mgr.config.profile_root / "work" / "data.txt").write_text("x")

    result = mgr.remove_profile("work", "codex-w")

    assert result.wrapper_path == mgr.config.bin_dir / "codex-w"
    assert result.wrapper_removed is True
    assert result.home_removed is True


def test_remove_profile_refuses_source_home(mgr: CodexAlias) -> None:
    mgr.config.source_home.mkdir(parents=True, exist_ok=True)
    (mgr.config.profile_root / "work").mkdir(parents=True, exist_ok=True)
    (mgr.config.profile_root / "work" / "config.toml").write_text("x")
    # Simulate a profile whose path is the configured source home.
    source = mgr.config.source_home
    cfg = Config(
        profile_root=source.parent,
        bin_dir=mgr.config.bin_dir,
        codex_cmd="codex",
        source_home=source,
        manager_bin_name="codexalias",
    )
    with pytest.raises(CodexAliasError, match="source home"):
        CodexAlias(cfg).remove_profile(source.name)


def test_remove_profile_refuses_current_home(mgr: CodexAlias, monkeypatch) -> None:
    mgr.add_profile("work")
    monkeypatch.setenv("CODEX_HOME", str(mgr.config.profile_root / "work"))

    with pytest.raises(CodexAliasError, match="CODEX_HOME"):
        mgr.remove_profile("work")


def test_remove_profile_missing_home_raises(mgr: CodexAlias) -> None:
    with pytest.raises(ProfileNotFoundError):
        mgr.remove_profile("work")


def test_run_argv_sets_isolated_home(mgr: CodexAlias) -> None:
    argv, env = mgr.run_argv("work", ["--", "--help"])
    assert argv == ["codex", "--", "--help"]
    assert env["CODEX_HOME"] == str(mgr.config.profile_root / "work")


def test_resume_argv_uses_configured_wrapper(mgr: CodexAlias) -> None:
    home = mgr.config.profile_root / "work"
    argv, env = mgr.resume_argv(home, "session-id", ["--yolo", "--model", "gpt-5"])
    assert argv == ["codex", "resume", "session-id", "--yolo", "--model", "gpt-5"]
    assert env["CODEX_HOME"] == str(home)


def test_run_argv_inherits_fish_codex_wrapper(mgr: CodexAlias, monkeypatch) -> None:
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")

    argv, _ = mgr.run_argv(
        "work", ["--dangerously-bypass-approvals-and-sandbox", "--version"]
    )

    assert argv == [
        "/opt/homebrew/bin/fish",
        "-ic",
        "codex $argv",
        "--",
        "--dangerously-bypass-approvals-and-sandbox",
        "--version",
    ]


@pytest.mark.parametrize("shell", ["bash", "zsh", "sh", "dash", "ksh"])
def test_run_argv_inherits_posix_shell_codex_wrapper(
    mgr: CodexAlias, monkeypatch, shell: str
) -> None:
    monkeypatch.setenv("SHELL", f"/bin/{shell}")

    argv, _ = mgr.run_argv("work", ["--model", "gpt-5"])

    assert argv == [
        f"/bin/{shell}",
        "-ic",
        'codex "$@"',
        "codex",
        "--model",
        "gpt-5",
    ]


def test_explicit_codex_wrapper_bypasses_shell(tmp_path, monkeypatch) -> None:
    config = Config(
        profile_root=tmp_path / "profiles",
        bin_dir=tmp_path / "bin",
        codex_cmd="codex",
        source_home=tmp_path / "source",
        manager_bin_name="codexalias",
        codex_wrapper="/tools/codex-wrapper",
    )
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")

    argv, _ = CodexAlias(config).run_argv("work", ["--version"])

    assert argv == ["/tools/codex-wrapper", "--version"]


def test_generated_wrapper_prefers_runtime_codex_wrapper(mgr: CodexAlias) -> None:
    target = mgr.add_profile("work")
    script = target.read_text()
    assert "codexalias" in script
    assert "CODEXALIAS_MANAGER_BIN_NAME" in script


def test_config_prefers_codex_wrapper(tmp_path) -> None:
    config = Config.from_env(
        {
            "HOME": str(tmp_path),
            "CODEXALIAS_CODEX_CMD": "real-codex",
            "CODEXALIAS_CODEX_WRAPPER": "/tools/codex-wrapper",
            "CODEXALIAS_CODEX_ARGS": '--flag "two words"',
        }
    )
    assert config.codex_cmd == "real-codex"
    assert config.codex_wrapper == "/tools/codex-wrapper"
    assert config.effective_codex_cmd == "/tools/codex-wrapper"
    assert config.codex_args == ("--flag", "two words")


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
