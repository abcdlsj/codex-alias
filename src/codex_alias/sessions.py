"""Session discovery and migration between Codex homes.

A Codex home stores conversations as ``sessions/**/*.jsonl`` plus a top-level
``history.jsonl`` index. This module reads and copies those artifacts without
any user interaction; the CLI drives selection/prompting on top.
"""

from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib

from .errors import (
    AmbiguousSessionError,
    HomeNotFoundError,
    SessionConflictError,
    SessionNotFoundError,
    SessionRepairError,
)
from .models import CopyStatus, SessionCopyResult, SessionFile, SessionFixResult

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


def configured_model_provider(home: Path) -> str:
    """Read the active top-level model provider from ``home/config.toml``."""
    config_path = home / "config.toml"
    if not config_path.is_file():
        raise SessionRepairError(
            f"model provider was not specified and config is missing: {config_path}"
        )
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SessionRepairError(f"failed to read config {config_path}: {exc}") from exc

    provider = data.get("model_provider")
    if not isinstance(provider, str) or not provider.strip():
        raise SessionRepairError(
            f"top-level model_provider is missing from config: {config_path}"
        )
    return provider.strip()


def _next_backup_path(path: Path) -> Path:
    number = 1
    while True:
        candidate = path.with_name(f"{path.name}.backup.{number}")
        if not candidate.exists():
            return candidate
        number += 1


def _replace_provider_field(
    container: object,
    key: str,
    provider: str,
    from_provider: str | None,
    previous: set[str],
) -> bool:
    if not isinstance(container, dict):
        return False
    old = container.get(key)
    if not isinstance(old, str) or old == provider:
        return False
    if from_provider is not None and old != from_provider:
        return False
    previous.add(old)
    container[key] = provider
    return True


def fix_session_provider(
    session: SessionFile,
    provider: str,
    *,
    from_provider: str | None = None,
    dry_run: bool = False,
) -> SessionFixResult:
    """Normalize persisted provider metadata in a Codex JSONL session.

    Every JSONL record is parsed before any write occurs. On a real repair the
    original is copied to a unique sibling backup and the replacement is
    written atomically. Only the two provider fields used by Codex session
    bootstrap are changed; conversation payloads are left untouched.
    """
    provider = provider.strip()
    if not provider:
        raise SessionRepairError("provider must not be empty")
    if from_provider is not None:
        from_provider = from_provider.strip()
        if not from_provider:
            raise SessionRepairError("from-provider must not be empty")

    try:
        path = session.path.resolve(strict=True)
        original_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeError) as exc:
        raise SessionRepairError(f"failed to read session {session.path}: {exc}") from exc

    rewritten: list[str] = []
    previous: set[str] = set()
    changed_records = 0
    changed_fields = 0

    for line_number, line in enumerate(original_lines, start=1):
        body = line.rstrip("\r\n")
        newline = line[len(body) :]
        if not body:
            raise SessionRepairError(
                f"invalid blank JSONL record at {path}:{line_number}"
            )
        try:
            record = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SessionRepairError(
                f"invalid JSONL record at {path}:{line_number}: {exc.msg}"
            ) from exc
        if not isinstance(record, dict):
            raise SessionRepairError(
                f"invalid non-object JSONL record at {path}:{line_number}"
            )

        fields_in_record = 0
        payload = record.get("payload")
        if record.get("type") == "session_meta":
            fields_in_record += int(
                _replace_provider_field(
                    payload, "model_provider", provider, from_provider, previous
                )
            )
        if record.get("type") == "event_msg" and isinstance(payload, dict):
            fields_in_record += int(
                _replace_provider_field(
                    payload.get("thread_settings"),
                    "model_provider_id",
                    provider,
                    from_provider,
                    previous,
                )
            )

        if fields_in_record:
            changed_records += 1
            changed_fields += fields_in_record
            rewritten.append(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + newline
            )
        else:
            rewritten.append(line)

    backup_path: Path | None = None
    if changed_fields and not dry_run:
        backup_path = _next_backup_path(path)
        try:
            shutil.copy2(path, backup_path)
            temp_name: str | None = None
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_name = temp_file.name
                temp_file.writelines(rewritten)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.chmod(temp_name, path.stat().st_mode)
            os.replace(temp_name, path)
        except OSError as exc:
            if "temp_name" in locals() and temp_name:
                Path(temp_name).unlink(missing_ok=True)
            raise SessionRepairError(f"failed to repair session {path}: {exc}") from exc

    return SessionFixResult(
        session_id=session.session_id,
        provider=provider,
        previous_providers=tuple(sorted(previous)),
        changed_records=changed_records,
        changed_fields=changed_fields,
        backup_path=backup_path,
        dry_run=dry_run,
    )


def fix_session_state_provider(
    home: Path,
    session_id: str,
    provider: str,
    *,
    from_provider: str | None = None,
    dry_run: bool = False,
) -> tuple[bool, Path | None]:
    """Repair the provider in Codex's SQLite thread index, when present.

    Codex 0.145 reads ``threads.model_provider`` during resume before replaying
    the JSONL rollout. A consistent SQLite online backup is created before the
    single conditional row update.
    """
    database = home / "state_5.sqlite"
    if not database.is_file():
        return False, None

    try:
        connection = sqlite3.connect(database)
        row = connection.execute(
            "SELECT model_provider FROM threads WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return False, None
        old_provider = row[0]
        if old_provider == provider:
            return False, None
        if from_provider is not None and old_provider != from_provider:
            return False, None
        if dry_run:
            return True, None

        backup_path = _next_backup_path(database)
        backup_connection = sqlite3.connect(backup_path)
        try:
            connection.backup(backup_connection)
        finally:
            backup_connection.close()

        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE threads SET model_provider = ? "
            "WHERE id = ? AND model_provider = ?",
            (provider, session_id, old_provider),
        )
        connection.commit()
        if cursor.rowcount != 1:
            raise SessionRepairError(
                f"thread state changed concurrently for session {session_id}"
            )
        return True, backup_path
    except sqlite3.Error as exc:
        raise SessionRepairError(
            f"failed to repair session state {database}: {exc}"
        ) from exc
    finally:
        if "connection" in locals():
            connection.close()
