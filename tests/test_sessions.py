from __future__ import annotations

import json
import sqlite3

import pytest

from codex_alias import CodexAlias
from codex_alias.errors import (
    SessionConflictError,
    SessionLossyMappingError,
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


def test_detect_last_session_infers_profile_and_final_output(
    mgr: CodexAlias, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    profile = mgr.config.profile_path("work")
    profile.mkdir(parents=True)
    cwd = tmp_path / "repo"
    cwd.mkdir()
    session_path = write_session(
        profile,
        SID_A,
        content=(
            json.dumps(
                {
                    "timestamp": "2026-08-27T10:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": SID_A,
                        "cwd": str(cwd),
                        "model_provider": "custom",
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "first"}],
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": "final answer",
                    },
                }
            )
            + "\n"
        ),
    )
    with sqlite3.connect(profile / "state_5.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, updated_at_ms INTEGER)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?)",
            (SID_A, str(session_path), str(cwd), 1_800_000_000_000),
        )

    monkeypatch.chdir(tmp_path)
    detected = mgr.detect_last_session(cwd)

    assert detected is not None
    assert detected.profile == "work"
    assert detected.session_id == SID_A
    assert detected.path == session_path
    assert detected.last_output == "final answer"


def test_detect_last_session_falls_back_to_rollout_metadata(
    mgr: CodexAlias, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    session_path = write_session(
        mgr.config.source_home,
        SID_B,
        content=json.dumps(
            {
                "timestamp": "2026-08-27T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": SID_B, "cwd": str(cwd)},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            }
        )
        + "\n",
    )
    monkeypatch.chdir(tmp_path)

    detected = mgr.detect_last_session(cwd)

    assert detected is not None
    assert detected.profile == "default"
    assert detected.session_id == SID_B
    assert detected.last_output == "done"


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
    state = json.loads(
        (mgr.config.profile_path("work") / ".codexalias.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["sync"]["types"] == ["sessions_shared"]


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


def _reasoning_session(
    home,
    session_id: str,
    provider: str = "opencode-go",
    encrypted_content: str | None = None,
):
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "session_id": session_id,
                "model_provider": provider,
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "id": "reasoning-1",
                "summary": [],
                "content": [
                    {"type": "reasoning_text", "text": "private reasoning"}
                ],
                "encrypted_content": encrypted_content,
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "public answer"}],
            },
        },
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


def test_fix_session_provider_updates_model_metadata(
    mgr: CodexAlias,
) -> None:
    path = _provider_session(mgr.config.source_home, SID_A)

    result = mgr.fix_session_provider(
        mgr.config.source_home,
        SID_A,
        "deepseek",
        model="deepseek-v4-pro",
    )

    assert result.changed_fields == 2
    assert result.changed_model_fields == 1
    assert result.previous_providers == ("aicoding", "custom")
    assert result.previous_models == ("gpt-5.6-sol",)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["payload"]["model_provider"] == "deepseek"
    assert records[1]["payload"]["thread_settings"] == {
        "model": "deepseek-v4-pro",
        "model_provider_id": "deepseek",
    }


def test_fix_session_provider_applies_compatibility_mapping(
    mgr: CodexAlias,
) -> None:
    path = _reasoning_session(
        mgr.config.source_home,
        SID_A,
        provider="fenno",
        encrypted_content="ciphertext",
    )
    original = path.read_text(encoding="utf-8")

    result = mgr.fix_session_provider(
        mgr.config.source_home, SID_A, "fenno", model="gpt-5.6-luna"
    )

    assert result.changed_fields == 0
    assert result.mapped_records == 1
    assert result.applied_mappings == ("gpt-5-empty-reasoning-content",)
    assert result.dropped_records == 0
    assert result.backup_path is not None
    assert result.backup_path.read_text(encoding="utf-8") == original
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[1]["payload"]["content"] == []
    assert records[1]["payload"]["encrypted_content"] == "ciphertext"
    assert records[2]["payload"]["content"] == [
        {"type": "output_text", "text": "public answer"}
    ]


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
        'model = "deepseek-v4-pro"\n'
        'model_provider = "custom"\n'
        '[model_providers.custom]\n'
        'name = "Custom"\n',
        encoding="utf-8",
    )
    assert mgr.configured_model_provider(mgr.config.source_home) == "custom"
    assert mgr.configured_model(mgr.config.source_home) == "deepseek-v4-pro"


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


def test_fix_session_provider_updates_sqlite_model(mgr: CodexAlias) -> None:
    _provider_session(mgr.config.source_home, SID_A)
    database = mgr.config.source_home / "state_5.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, model_provider TEXT NOT NULL, model TEXT NOT NULL"
            ")"
        )
        connection.execute(
            "INSERT INTO threads (id, model_provider, model) VALUES (?, ?, ?)",
            (SID_A, "aicoding", "gpt-5.6-sol"),
        )

    result = mgr.fix_session_provider(
        mgr.config.source_home,
        SID_A,
        "deepseek",
        model="deepseek-v4-pro",
    )

    assert result.state_changed is True
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT model_provider, model FROM threads WHERE id = ?", (SID_A,)
        ).fetchone() == ("deepseek", "deepseek-v4-pro")


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


def _create_thread_database(home, session_id: str, rollout_path, provider: str) -> None:
    database = home / "state_5.sqlite"
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                source TEXT NOT NULL, model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL, title TEXT NOT NULL
            );
            CREATE TABLE thread_dynamic_tools (
                thread_id TEXT NOT NULL, position INTEGER NOT NULL,
                name TEXT NOT NULL, description TEXT NOT NULL,
                input_schema TEXT NOT NULL, defer_loading INTEGER NOT NULL DEFAULT 0,
                namespace TEXT, PRIMARY KEY(thread_id, position)
            );
            """
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, 1, 1, 'cli', ?, '/repo', 'title')",
            (session_id, str(rollout_path), provider),
        )


def test_clone_session_for_profile_uses_new_id_and_preserves_source(
    mgr: CodexAlias, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = mgr.config.source_home
    source_path = _provider_session(source, SID_A)
    original = source_path.read_bytes()
    _create_thread_database(source, SID_A, source_path, "custom")
    monkeypatch.setenv("HOME", str(source.parent))
    # default_source_home resolves to HOME/.codex, so make the configured source
    # discoverable under that canonical location for this test.
    default_home = source.parent / ".codex"
    default_home.symlink_to(source, target_is_directory=True)

    target = mgr.config.profile_path("cpa")
    target.mkdir(parents=True)
    (target / "config.toml").write_text('model_provider = "cpa"\n')
    _create_thread_database(target, SID_B, target / "other.jsonl", "cpa")

    result = mgr.clone_session_for_profile(SID_A, target)

    assert result.session_id != SID_A
    assert result.provider == "cpa"
    assert source_path.read_bytes() == original
    records = [json.loads(line) for line in result.path.read_text().splitlines()]
    assert records[0]["payload"]["id"] == result.session_id
    assert records[0]["payload"]["model_provider"] == "cpa"
    assert records[1]["payload"]["thread_settings"]["model_provider_id"] == "cpa"
    with sqlite3.connect(target / "state_5.sqlite") as connection:
        assert connection.execute(
            "SELECT model_provider FROM threads WHERE id = ?",
            (result.session_id,),
        ).fetchone() == ("cpa",)


def test_clone_session_preserves_unicode_line_separators_in_json_strings(
    mgr: CodexAlias, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = mgr.config.source_home
    source_path = write_session(
        source,
        SID_A,
        content=(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": SID_A, "model_provider": "custom"},
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {"text": "before\u0085after"},
                },
                ensure_ascii=False,
            )
            + "\n"
        ),
    )
    original = source_path.read_bytes()
    monkeypatch.setenv("HOME", str(source.parent))
    (source.parent / ".codex").symlink_to(source, target_is_directory=True)

    target = mgr.config.profile_path("fenno")
    target.mkdir(parents=True)
    (target / "config.toml").write_text('model_provider = "fenno"\n')

    result = mgr.clone_session_for_profile(SID_A, target)

    assert source_path.read_bytes() == original
    records = [
        json.loads(line)
        for line in result.path.read_text(encoding="utf-8").split("\n")
        if line
    ]
    assert records[1]["payload"]["text"] == "before\u0085after"


def test_clone_session_works_when_target_storage_is_shared(
    mgr: CodexAlias, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = mgr.config.source_home
    source_path = _provider_session(source, SID_A)
    _create_thread_database(source, SID_A, source_path, "custom")
    monkeypatch.setenv("HOME", str(source.parent))
    (source.parent / ".codex").symlink_to(source, target_is_directory=True)

    target = mgr.config.profile_path("cpa")
    target.mkdir(parents=True)
    (target / "config.toml").write_text('model_provider = "cpa"\n')
    (target / "sessions").symlink_to(source / "sessions", target_is_directory=True)
    (target / "state_5.sqlite").symlink_to(source / "state_5.sqlite")

    result = mgr.clone_session_for_profile(SID_A, target)

    assert result.path.is_file()
    assert result.session_id != SID_A
    with sqlite3.connect(source / "state_5.sqlite") as connection:
        rows = connection.execute(
            "SELECT id, model_provider FROM threads ORDER BY id"
        ).fetchall()
    assert (SID_A, "custom") in rows
    assert (result.session_id, "cpa") in rows


def test_clone_session_applies_target_provider_compatibility_mapping(
    mgr: CodexAlias, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = mgr.config.source_home
    source_path = _reasoning_session(source, SID_A)
    original = source_path.read_bytes()
    monkeypatch.setenv("HOME", str(source.parent))
    (source.parent / ".codex").symlink_to(source, target_is_directory=True)

    target = mgr.config.profile_path("gateway")
    target.mkdir(parents=True)
    (target / "config.toml").write_text(
        'model_provider = "gateway"\nmodel = "gpt-5.6-luna"\n',
        encoding="utf-8",
    )

    result = mgr.clone_session_for_profile(SID_A, target)

    assert source_path.read_bytes() == original
    assert result.mapped_records == 1
    assert result.applied_mappings == ("gpt-5-empty-reasoning-content",)
    assert result.provider == "gateway"
    assert result.model == "gpt-5.6-luna"
    records = [json.loads(line) for line in result.path.read_text().splitlines()]
    assert records[1]["payload"]["content"] == []
    assert records[1]["payload"]["encrypted_content"] is None
    assert records[2]["payload"]["content"] == [
        {"type": "output_text", "text": "public answer"}
    ]


def test_clone_session_preserves_reasoning_for_unmapped_provider(
    mgr: CodexAlias, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = mgr.config.source_home
    _reasoning_session(source, SID_A)
    monkeypatch.setenv("HOME", str(source.parent))
    (source.parent / ".codex").symlink_to(source, target_is_directory=True)

    target = mgr.config.profile_path("fenno")
    target.mkdir(parents=True)
    (target / "config.toml").write_text(
        'model_provider = "fenno"\nmodel = "deepseek-v4-flash"\n',
        encoding="utf-8",
    )

    result = mgr.clone_session_for_profile(SID_A, target)

    assert result.mapped_records == 0
    assert result.applied_mappings == ()
    assert result.provider == "fenno"
    assert result.model == "deepseek-v4-flash"
    records = [json.loads(line) for line in result.path.read_text().splitlines()]
    assert records[1]["payload"]["content"] == [
        {"type": "reasoning_text", "text": "private reasoning"}
    ]


def _write_provider_config(
    home,
    active_provider: str,
    model: str,
    definitions: dict[str, str],
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    lines = [
        f'model_provider = "{active_provider}"',
        f'model = "{model}"',
    ]
    for provider, base_url in definitions.items():
        lines.extend(
            [
                f'[model_providers.{provider}]',
                f'base_url = "{base_url}"',
                'wire_api = "responses"',
            ]
        )
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_clone_session_keeps_encrypted_reasoning_for_same_backend_alias(
    mgr: CodexAlias, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = mgr.config.source_home
    _reasoning_session(
        source, SID_A, provider="source_alias", encrypted_content="ciphertext"
    )
    _write_provider_config(
        source,
        "source_alias",
        "gpt-5.4",
        {"source_alias": "https://same.example/v1/"},
    )
    monkeypatch.setenv("HOME", str(source.parent))
    (source.parent / ".codex").symlink_to(source, target_is_directory=True)

    target = mgr.config.profile_path("target")
    _write_provider_config(
        target,
        "target_alias",
        "gpt-5.4",
        {"target_alias": "https://same.example/v1"},
    )

    result = mgr.clone_session_for_profile(SID_A, target)

    assert result.dropped_records == 0
    assert result.lossy_mappings == ()
    records = [json.loads(line) for line in result.path.read_text().splitlines()]
    assert records[1]["payload"]["content"] == []
    assert records[1]["payload"]["encrypted_content"] == "ciphertext"


def test_clone_session_clears_encrypted_reasoning_across_known_backends(
    mgr: CodexAlias, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = mgr.config.source_home
    _reasoning_session(
        source, SID_A, provider="source_api", encrypted_content="ciphertext"
    )
    _write_provider_config(
        source,
        "source_api",
        "gpt-5.5",
        {"source_api": "https://source.example/v1"},
    )
    monkeypatch.setenv("HOME", str(source.parent))
    (source.parent / ".codex").symlink_to(source, target_is_directory=True)

    target = mgr.config.profile_path("target")
    _write_provider_config(
        target,
        "target_api",
        "gpt-5.6-sol",
        {"target_api": "https://target.example/v1"},
    )

    with pytest.raises(SessionLossyMappingError):
        mgr.clone_session_for_profile(SID_A, target, allow_lossy=False)
    assert not list((target / "sessions").rglob("*.jsonl"))

    result = mgr.clone_session_for_profile(SID_A, target)

    assert result.mapped_records == 1
    assert result.dropped_records == 0
    assert result.lossy_mappings == (
        "foreign-backend-clear-encrypted-reasoning",
    )
    records = [json.loads(line) for line in result.path.read_text().splitlines()]
    reasoning = [
        record for record in records if record.get("payload", {}).get("type") == "reasoning"
    ]
    assert len(reasoning) == 1
    assert reasoning[0]["payload"]["encrypted_content"] is None
    assert any(
        record.get("payload", {}).get("type") == "message" for record in records
    )


def test_clone_session_preserves_paginated_ordinals_and_rewrites_thread_ids(
    mgr: CodexAlias, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = mgr.config.source_home
    records = [
        {
            "timestamp": "2026-08-17T05:03:13.364Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {
                "session_id": SID_A,
                "id": SID_A,
                "model_provider": "source_api",
                "history_mode": "paginated",
            },
        },
        {
            "timestamp": "2026-08-17T05:03:13.365Z",
            "ordinal": 1,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": SID_A,
                "item": {"session_id": SID_A},
            },
        },
        {
            "timestamp": "2026-08-17T05:03:13.366Z",
            "ordinal": 2,
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "id": "reasoning-1",
                "summary": [],
                "encrypted_content": "foreign-ciphertext",
            },
        },
        {
            "timestamp": "2026-08-17T05:03:13.367Z",
            "ordinal": 3,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": SID_A,
                "items": [{"thread_id": SID_A}],
            },
        },
    ]
    write_session(
        source,
        SID_A,
        content="".join(json.dumps(record) + "\n" for record in records),
    )
    _write_provider_config(
        source,
        "source_api",
        "gpt-5.5",
        {"source_api": "https://source.example/v1"},
    )
    monkeypatch.setenv("HOME", str(source.parent))
    (source.parent / ".codex").symlink_to(source, target_is_directory=True)

    target = mgr.config.profile_path("target")
    _write_provider_config(
        target,
        "target_api",
        "gpt-5.6-sol",
        {"target_api": "https://target.example/v1"},
    )

    result = mgr.clone_session_for_profile(SID_A, target)

    copied = [json.loads(line) for line in result.path.read_text().splitlines()]
    assert [record["ordinal"] for record in copied] == [0, 1, 2, 3]
    assert copied[2]["payload"]["encrypted_content"] is None
    assert copied[0]["payload"]["id"] == result.session_id
    assert copied[0]["payload"]["session_id"] == result.session_id
    assert copied[1]["payload"]["thread_id"] == result.session_id
    assert copied[1]["payload"]["item"]["session_id"] == SID_A
    assert copied[3]["payload"]["thread_id"] == result.session_id
    assert copied[3]["payload"]["items"][0]["thread_id"] == SID_A


def test_fix_session_provider_normalizes_paginated_ordinals(mgr: CodexAlias) -> None:
    path = write_session(
        mgr.config.source_home,
        SID_A,
        content="".join(
            json.dumps(record) + "\n"
            for record in (
                {
                    "ordinal": 4,
                    "type": "session_meta",
                    "payload": {
                        "id": SID_A,
                        "model_provider": "custom",
                        "history_mode": "paginated",
                    },
                },
                {
                    "ordinal": 9,
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant"},
                },
            )
        ),
    )

    result = mgr.fix_session_provider(mgr.config.source_home, SID_A, "custom")

    assert result.changed_records == 2
    assert [json.loads(line)["ordinal"] for line in path.read_text().splitlines()] == [0, 1]


def test_clone_session_does_not_guess_when_source_backend_is_unknown(
    mgr: CodexAlias, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = mgr.config.source_home
    _reasoning_session(
        source, SID_A, provider="missing", encrypted_content="ciphertext"
    )
    monkeypatch.setenv("HOME", str(source.parent))
    (source.parent / ".codex").symlink_to(source, target_is_directory=True)
    target = mgr.config.profile_path("target")
    _write_provider_config(
        target,
        "target_api",
        "gpt-5.6-sol",
        {"target_api": "https://target.example/v1"},
    )

    result = mgr.clone_session_for_profile(SID_A, target)

    assert result.dropped_records == 0
    assert result.mapping_warnings == (
        "could not verify portability of 1 encrypted reasoning item(s) because "
        "a backend fingerprint is missing; records were preserved",
    )
    records = [json.loads(line) for line in result.path.read_text().splitlines()]
    assert records[1]["payload"]["encrypted_content"] == "ciphertext"


def test_clone_session_blocks_foreign_encrypted_compaction(
    mgr: CodexAlias, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = mgr.config.source_home
    records = [
        {
            "type": "session_meta",
            "payload": {"id": SID_A, "model_provider": "source_api"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "compaction",
                "encrypted_content": "opaque-history",
            },
        },
    ]
    write_session(
        source,
        SID_A,
        content="".join(json.dumps(record) + "\n" for record in records),
    )
    _write_provider_config(
        source,
        "source_api",
        "gpt-5.5",
        {"source_api": "https://source.example/v1"},
    )
    monkeypatch.setenv("HOME", str(source.parent))
    (source.parent / ".codex").symlink_to(source, target_is_directory=True)
    target = mgr.config.profile_path("target")
    _write_provider_config(
        target,
        "target_api",
        "gpt-5.6-sol",
        {"target_api": "https://target.example/v1"},
    )

    with pytest.raises(SessionRepairError, match="encrypted compaction"):
        mgr.clone_session_for_profile(SID_A, target)

    assert not list((target / "sessions").rglob("*.jsonl"))


def test_fix_session_reports_incomplete_orphan_tool_call_without_deleting_it(
    mgr: CodexAlias,
) -> None:
    records = [
        {
            "type": "session_meta",
            "payload": {"id": SID_A, "model_provider": "custom"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "call-1",
                "name": "shell",
                "status": "incomplete",
            },
        },
    ]
    path = write_session(
        mgr.config.source_home,
        SID_A,
        content="".join(json.dumps(record) + "\n" for record in records),
    )
    original = path.read_bytes()

    result = mgr.fix_session_provider(
        mgr.config.source_home, SID_A, "custom", model="deepseek-v4-flash"
    )

    assert result.mapping_warnings == (
        "1 historical tool call(s) have no output",
        "1 historical tool call(s) are marked incomplete",
    )
    assert result.mapped_records == 0
    assert result.backup_path is None
    assert path.read_bytes() == original


def test_clone_session_allows_profile_without_state_database(
    mgr: CodexAlias, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = mgr.config.source_home
    _provider_session(source, SID_A)
    monkeypatch.setenv("HOME", str(source.parent))
    (source.parent / ".codex").symlink_to(source, target_is_directory=True)

    target = mgr.config.profile_path("fresh")
    target.mkdir(parents=True)
    (target / "config.toml").write_text('model_provider = "fresh"\n')

    result = mgr.clone_session_for_profile(SID_A, target)

    assert result.path.is_file()
    assert result.provider == "fresh"
    assert not (target / "state_5.sqlite").exists()
