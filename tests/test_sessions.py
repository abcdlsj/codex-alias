from __future__ import annotations

import pytest

from codex_alias import CodexAlias
from codex_alias.errors import SessionConflictError, SessionNotFoundError
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
