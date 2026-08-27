from __future__ import annotations

from pathlib import Path

import pytest

from codex_alias import CodexAlias, Config


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    """A Config pinned entirely inside tmp_path so tests never touch $HOME."""
    return Config(
        profile_root=tmp_path / "profiles",
        bin_dir=tmp_path / "bin",
        codex_cmd="codex",
        source_home=tmp_path / "source",
        manager_bin_name="codexalias",
    )


@pytest.fixture(autouse=True)
def isolate_cli_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep CLI tests from creating profiles or wrappers in the real home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("CODEXALIAS_SOURCE_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("CODEXALIAS_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("CODEXALIAS_BIN_DIR", str(tmp_path / "bin"))


@pytest.fixture()
def mgr(config: Config, monkeypatch: pytest.MonkeyPatch) -> CodexAlias:
    # Keep both CODEX_HOME and the canonical HOME/.codex source isolated.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(config.source_home.parent))
    # Tests opt into shell-aware launching explicitly when relevant.
    monkeypatch.delenv("SHELL", raising=False)
    return CodexAlias(config)


def write_session(home: Path, session_id: str, *, content: str = "line\n") -> Path:
    """Create a session file mimicking Codex's YYYY/MM/DD layout."""
    session_dir = home / "sessions" / "2026" / "07" / "27"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"rollout-2026-07-27T10-00-00-{session_id}.jsonl"
    path.write_text(content, encoding="utf-8")
    return path
