"""Session discovery and migration between Codex homes.

A Codex home stores conversations as ``sessions/**/*.jsonl`` plus a top-level
``history.jsonl`` index. This module reads and copies those artifacts without
any user interaction; the CLI drives selection/prompting on top.
"""

from __future__ import annotations

import filecmp
import re
import shutil
from pathlib import Path

from .errors import (
    AmbiguousSessionError,
    HomeNotFoundError,
    SessionConflictError,
    SessionNotFoundError,
)
from .models import CopyStatus, SessionCopyResult, SessionFile

_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def session_id_from_path(path: Path) -> str | None:
    """Extract a trailing UUID session id from a ``*.jsonl`` filename."""
    match = _UUID_RE.search(path.stem)
    return match.group(1) if match else None


def _sessions_root(home: Path) -> Path:
    return home / "sessions"


def list_session_files(home: Path) -> list[SessionFile]:
    """All sessions under ``home``, newest filename first.

    Missing session stores yield an empty list rather than raising, so callers
    can treat "no sessions" and "no store" the same way.
    """
    root = _sessions_root(home)
    if not root.is_dir():
        return []

    files = sorted(
        (p for p in root.rglob("*.jsonl") if p.is_file()),
        key=lambda p: str(p),
        reverse=True,
    )
    out: list[SessionFile] = []
    for path in files:
        sid = session_id_from_path(path)
        if sid is None:
            continue
        out.append(
            SessionFile(
                session_id=sid,
                path=path,
                relative_path=str(path.relative_to(root)),
            )
        )
    return out


def resolve_session_file(home: Path, query: str) -> SessionFile:
    """Locate a single session in ``home`` by id, filename, or path fragment.

    Raises :class:`SessionNotFoundError` when nothing matches and
    :class:`AmbiguousSessionError` when more than one file matches.
    """
    root = _sessions_root(home)
    if not root.is_dir():
        raise HomeNotFoundError(f"session store not found: {root}")

    files = list_session_files(home)

    # Exact id match is unambiguous and wins outright.
    for sf in files:
        if sf.session_id == query:
            return sf

    # Direct path / relative path hit.
    as_path = Path(query)
    for sf in files:
        if sf.path == as_path or sf.relative_path == query:
            return sf

    # Fall back to substring matching on path or relative path.
    matches = [
        sf
        for sf in files
        if query in str(sf.path) or query in sf.relative_path
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SessionNotFoundError(f"session not found in {home}: {query}")
    raise AmbiguousSessionError(query, [m.relative_path for m in matches])


def _append_history(src_home: Path, dst_home: Path, session_id: str) -> None:
    """Copy this session's history lines into the target, de-duplicated."""
    src_history = src_home / "history.jsonl"
    if not src_history.is_file():
        return

    needle = f'"session_id":"{session_id}"'
    new_lines = [
        line
        for line in src_history.read_text(encoding="utf-8").splitlines()
        if line and needle in line
    ]
    if not new_lines:
        return

    dst_home.mkdir(parents=True, exist_ok=True)
    dst_history = dst_home / "history.jsonl"
    existing = set()
    if dst_history.is_file():
        existing = set(dst_history.read_text(encoding="utf-8").splitlines())

    to_add = [line for line in new_lines if line not in existing]
    if not to_add:
        return
    with dst_history.open("a", encoding="utf-8") as fh:
        for line in to_add:
            fh.write(line + "\n")


def copy_session(
    src_home: Path, session: SessionFile, dst_home: Path
) -> SessionCopyResult:
    """Copy one session file (and its history) into ``dst_home``.

    Idempotent: an identical target is skipped; a divergent target raises
    :class:`SessionConflictError` rather than clobbering data.
    """
    if not session.path.is_file():
        raise SessionNotFoundError(f"session file not found: {session.path}")

    dst_file = _sessions_root(dst_home) / session.relative_path
    dst_file.parent.mkdir(parents=True, exist_ok=True)

    if dst_file.exists():
        if filecmp.cmp(session.path, dst_file, shallow=False):
            _append_history(src_home, dst_home, session.session_id)
            return SessionCopyResult(session.session_id, CopyStatus.SKIPPED)
        raise SessionConflictError(
            f"target session already exists with different content: {dst_file}"
        )

    shutil.copyfile(session.path, dst_file)
    _append_history(src_home, dst_home, session.session_id)
    return SessionCopyResult(session.session_id, CopyStatus.COPIED)


def copy_all_sessions(src_home: Path, dst_home: Path) -> list[SessionCopyResult]:
    """Copy every session from ``src_home`` into ``dst_home``."""
    results: list[SessionCopyResult] = []
    for session in list_session_files(src_home):
        results.append(copy_session(src_home, session, dst_home))
    return results
