from __future__ import annotations

import json
import sqlite3

import pytest

from codex_alias import CodexAlias
from codex_alias.errors import (
    SessionConflictError,
    SessionNotFoundError,
    SessionRepairError,
)
from codex_alias.models import CopyStatus
from conftest import write_session

SID_A = "019d1df0-8f1e-7393-b54a-0f0b511c5a33"
SID_B = "019d1ec4-548a-7083-992a-c807fd0b5c8e"


def test_list_and_resolve_session(mgr: CodexAlias) -> None:
    src = mgr.config.source_home
    write_session(src, SID_A)
    sessions = mgr.list_sessions(src)
    assert [s.session_id for s in sessions] == [SID_A]
    assert mgr.resolve_session(src, SID_A).session_id == SID_A


def test_resolve_missing_session_raises(mgr: CodexAlias) -> None:
    mgr.config.source_home.mkdir(parents=True)
    write_session(mgr.config.source_home, SID_A)
    with pytest.raises(SessionNotFoundError):
        mgr.resolve_session(mgr.config.source_home, "does-not-exist")


def test_copy_session_and_history(mgr: CodexAlias) -> None:
    src = mgr.config.source_home
    write_session(src, SID_A)
    (src / "history.jsonl").write_text(
        f'{{"session_id":"{SID_A}","text":"hi"}}\n'
        f'{{"session_id":"{SID_B}","text":"other"}}\n',
        encoding="utf-8",
    )
    dst = mgr.config.profile_path("work")

    result = mgr.copy_session_by_query(src, SID_A, dst)
    assert result.status is CopyStatus.COPIED

    copied = dst / "sessions" / "2026" / "07" / "27"
    assert list(copied.glob("*.jsonl"))
    # only the matching session's history line is carried over
    history = (dst / "history.jsonl").read_text()
    assert SID_A in history
    assert SID_B not in history


def test_copy_is_idempotent(mgr: CodexAlias) -> None:
    src = mgr.config.source_home
    write_session(src, SID_A)
    dst = mgr.config.profile_path("work")

    first = mgr.copy_session_by_query(src, SID_A, dst)
    second = mgr.copy_session_by_query(src, SID_A, dst)
    assert first.status is CopyStatus.COPIED
    assert second.status is CopyStatus.SKIPPED


def test_copy_conflict_on_divergent_content(mgr: CodexAlias) -> None:
    src = mgr.config.source_home
    write_session(src, SID_A, content="original\n")
    dst = mgr.config.profile_path("work")
    mgr.copy_session_by_query(src, SID_A, dst)

    # mutate source content -> same path, different bytes -> conflict
    write_session(src, SID_A, content="tampered\n")
    with pytest.raises(SessionConflictError):
        mgr.copy_session_by_query(src, SID_A, dst)


def test_copy_all_sessions(mgr: CodexAlias) -> None:
    src = mgr.config.source_home
    write_session(src, SID_A)
    write_session(src, SID_B)
    dst = mgr.config.profile_path("work")

    results = mgr.copy_all_sessions(src, dst)
    assert len(results) == 2
    assert all(r.status is CopyStatus.COPIED for r in results)


def test_share_sessions_symlinks(mgr: CodexAlias) -> None:
    src = mgr.config.source_home
    write_session(src, SID_A)
    (src / "history.jsonl").write_text("x\n", encoding="utf-8")
    mgr.add_profile("work")

    actions = mgr.share_sessions("work", "@source")
    assert actions
    link = mgr.config.profile_path("work") / "sessions"
    assert link.is_symlink()
    assert link.resolve() == (src / "sessions").resolve()
    assert mgr.list_profiles()[0].sessions_shared is True


def _provider_session(home, session_id: str) -> object:
    records = [
        {
            "type": "session_meta",
            "payload": {"id": session_id, "model_provider": "custom"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "thread_settings",
                "thread_settings": {
                    "model": "gpt-5.6-sol",
                    "model_provider_id": "aicoding",
                },
            },
        },
        {"type": "response_item", "payload": {"text": "keep me unchanged"}},
    ]
    content = "".join(json.dumps(record) + "\n" for record in records)
    return write_session(home, session_id, content=content)


def test_fix_session_provider_creates_backup_and_only_changes_metadata(
    mgr: CodexAlias,
) -> None:
    path = _provider_session(mgr.config.source_home, SID_A)
    original = path.read_text(encoding="utf-8")

    result = mgr.fix_session_provider(
        mgr.config.source_home,
        SID_A,
        "custom",
        from_provider="aicoding",
    )

    assert result.changed_records == 1
    assert result.changed_fields == 1
    assert result.previous_providers == ("aicoding",)
    assert result.backup_path is not None
    assert result.backup_path.read_text(encoding="utf-8") == original
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[1]["payload"]["thread_settings"]["model_provider_id"] == "custom"
    assert records[2]["payload"]["text"] == "keep me unchanged"


def test_fix_session_provider_dry_run_does_not_write(mgr: CodexAlias) -> None:
    path = _provider_session(mgr.config.source_home, SID_A)
    original = path.read_text(encoding="utf-8")

    result = mgr.fix_session_provider(
        mgr.config.source_home, SID_A, "custom", dry_run=True
    )

    assert result.changed_fields == 1
    assert result.backup_path is None
    assert path.read_text(encoding="utf-8") == original
    assert not list(path.parent.glob(f"{path.name}.backup.*"))


def test_fix_session_provider_rejects_invalid_json_without_writing(
    mgr: CodexAlias,
) -> None:
    path = write_session(
        mgr.config.source_home,
        SID_A,
        content='{"type":"session_meta","payload":{"model_provider":"old"}}\ninvalid\n',
    )
    original = path.read_text(encoding="utf-8")

    with pytest.raises(SessionRepairError, match="invalid JSONL record"):
        mgr.fix_session_provider(mgr.config.source_home, SID_A, "custom")

    assert path.read_text(encoding="utf-8") == original
    assert not list(path.parent.glob(f"{path.name}.backup.*"))


def test_configured_model_provider(mgr: CodexAlias) -> None:
    mgr.config.source_home.mkdir(parents=True)
    (mgr.config.source_home / "config.toml").write_text(
        'model_provider = "custom"\n[model_providers.custom]\nname = "Custom"\n',
        encoding="utf-8",
    )
    assert mgr.configured_model_provider(mgr.config.source_home) == "custom"


def test_fix_session_provider_updates_sqlite_thread_state(mgr: CodexAlias) -> None:
    _provider_session(mgr.config.source_home, SID_A)
    database = mgr.config.source_home / "state_5.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO threads (id, model_provider) VALUES (?, ?)",
            (SID_A, "aicoding"),
        )

    result = mgr.fix_session_provider(
        mgr.config.source_home,
        SID_A,
        "custom",
        from_provider="aicoding",
    )

    assert result.state_changed is True
    assert result.state_backup_path is not None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT model_provider FROM threads WHERE id = ?", (SID_A,)
        ).fetchone() == ("custom",)
    with sqlite3.connect(result.state_backup_path) as connection:
        assert connection.execute(
            "SELECT model_provider FROM threads WHERE id = ?", (SID_A,)
        ).fetchone() == ("aicoding",)


def test_fix_session_provider_sqlite_dry_run_does_not_write(mgr: CodexAlias) -> None:
    _provider_session(mgr.config.source_home, SID_A)
    database = mgr.config.source_home / "state_5.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO threads (id, model_provider) VALUES (?, ?)",
            (SID_A, "aicoding"),
        )

    result = mgr.fix_session_provider(
        mgr.config.source_home,
        SID_A,
        "custom",
        from_provider="aicoding",
        dry_run=True,
    )

    assert result.state_changed is True
    assert result.state_backup_path is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT model_provider FROM threads WHERE id = ?", (SID_A,)
        ).fetchone() == ("aicoding",)
