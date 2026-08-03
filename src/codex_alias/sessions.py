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
import time
import uuid
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
from .models import (
    CopyStatus,
    SessionCloneResult,
    SessionCopyResult,
    SessionFile,
    SessionFixResult,
)

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


def _read_config(home: Path) -> dict[str, object]:
    """Read a Codex config file as a TOML object."""
    config_path = home / "config.toml"
    if not config_path.is_file():
        raise SessionRepairError(
            f"Codex config is missing: {config_path}"
        )
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SessionRepairError(f"failed to read config {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SessionRepairError(f"invalid Codex config: {config_path}")
    return data


def configured_model_provider(home: Path) -> str:
    """Read the active top-level model provider from ``home/config.toml``."""
    config_path = home / "config.toml"
    data = _read_config(home)

    provider = data.get("model_provider")
    if not isinstance(provider, str) or not provider.strip():
        raise SessionRepairError(
            f"top-level model_provider is missing from config: {config_path}"
        )
    return provider.strip()


def configured_model(home: Path) -> str:
    """Read the active top-level model from ``home/config.toml``."""
    config_path = home / "config.toml"
    data = _read_config(home)

    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        raise SessionRepairError(
            f"top-level model is missing from config: {config_path}"
        )
    return model.strip()


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


def _replace_model_field(
    container: object,
    model: str,
    previous: set[str],
) -> bool:
    if not isinstance(container, dict):
        return False
    old = container.get("model")
    if old == model:
        return False
    if isinstance(old, str):
        previous.add(old)
    container["model"] = model
    return True


def fix_session_provider(
    session: SessionFile,
    provider: str,
    *,
    model: str | None = None,
    from_provider: str | None = None,
    dry_run: bool = False,
) -> SessionFixResult:
    """Normalize persisted provider and model metadata in a Codex session.

    Every JSONL record is parsed before any write occurs. On a real repair the
    original is copied to a unique sibling backup and the replacement is
    written atomically. Only the two provider fields used by Codex session
    bootstrap and the thread model are changed; conversation payloads are left
    untouched.
    """
    provider = provider.strip()
    if not provider:
        raise SessionRepairError("provider must not be empty")
    if model is not None:
        model = model.strip()
        if not model:
            raise SessionRepairError("model must not be empty")
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
    previous_models: set[str] = set()
    changed_records = 0
    changed_fields = 0
    changed_model_fields = 0

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
        model_fields_in_record = 0
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
            if model is not None:
                model_fields_in_record += int(
                    _replace_model_field(
                        payload.get("thread_settings"), model, previous_models
                    )
                )

        if fields_in_record or model_fields_in_record:
            changed_records += 1
            changed_fields += fields_in_record
            changed_model_fields += model_fields_in_record
            rewritten.append(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + newline
            )
        else:
            rewritten.append(line)

    backup_path: Path | None = None
    if (changed_fields or changed_model_fields) and not dry_run:
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
        model=model,
        previous_models=tuple(sorted(previous_models)),
        changed_model_fields=changed_model_fields,
    )


def fix_session_state_provider(
    home: Path,
    session_id: str,
    provider: str,
    *,
    model: str | None = None,
    from_provider: str | None = None,
    dry_run: bool = False,
) -> tuple[bool, Path | None]:
    """Repair provider/model values in Codex's SQLite thread index, when present.

    Codex 0.145 reads ``threads.model_provider`` during resume before replaying
    the JSONL rollout. A consistent SQLite online backup is created before the
    single conditional row update.
    """
    database = home / "state_5.sqlite"
    if not database.is_file():
        return False, None

    try:
        connection = sqlite3.connect(database)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(threads)")
        }
        if "model_provider" not in columns:
            return False, None
        selected_columns = ["model_provider"]
        if model is not None and "model" in columns:
            selected_columns.append("model")
        row = connection.execute(
            f"SELECT {', '.join(selected_columns)} FROM threads WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return False, None
        old_provider = row[0]
        if from_provider is not None and old_provider != from_provider:
            return False, None
        provider_changed = old_provider != provider
        model_changed = (
            model is not None
            and "model" in columns
            and row[1] != model
        )
        if not provider_changed and not model_changed:
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
        assignments: list[str] = []
        assignment_values: list[object] = []
        if provider_changed:
            assignments.append("model_provider = ?")
            assignment_values.append(provider)
        if model_changed:
            assignments.append("model = ?")
            assignment_values.append(model)
        where = "id = ?"
        where_values: list[object] = [session_id]
        if provider_changed:
            where += " AND model_provider = ?"
            where_values.append(old_provider)
        if model_changed:
            where += " AND model = ?"
            where_values.append(row[1])
        cursor = connection.execute(
            f"UPDATE threads SET {', '.join(assignments)} WHERE {where}",
            [*assignment_values, *where_values],
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


def _clone_jsonl(
    session: SessionFile, dst_home: Path, new_id: str, provider: str
) -> Path:
    """Create a validated session copy with a new identity and provider."""
    try:
        lines = session.path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeError) as exc:
        raise SessionRepairError(f"failed to read session {session.path}: {exc}") from exc

    rewritten: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        body = line.rstrip("\r\n")
        newline = line[len(body) :]
        try:
            record = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SessionRepairError(
                f"invalid JSONL record at {session.path}:{line_number}: {exc.msg}"
            ) from exc
        if not isinstance(record, dict):
            raise SessionRepairError(
                f"invalid non-object JSONL record at {session.path}:{line_number}"
            )
        payload = record.get("payload")
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            if payload.get("id") == session.session_id:
                payload["id"] = new_id
            if payload.get("session_id") == session.session_id:
                payload["session_id"] = new_id
            payload["model_provider"] = provider
        if record.get("type") == "event_msg" and isinstance(payload, dict):
            settings = payload.get("thread_settings")
            if isinstance(settings, dict) and "model_provider_id" in settings:
                settings["model_provider_id"] = provider
        rewritten.append(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + newline
        )

    source_name = session.path.name
    if session.session_id not in source_name:
        raise SessionRepairError(f"session id is missing from filename: {session.path}")
    target_dir = _sessions_root(dst_home) / Path(session.relative_path).parent
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / source_name.replace(session.session_id, new_id)
    if target_path.exists():
        raise SessionConflictError(f"cloned session already exists: {target_path}")

    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target_dir, delete=False
        ) as temp_file:
            temp_name = temp_file.name
            temp_file.writelines(rewritten)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, target_path)
    except OSError as exc:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
        raise SessionRepairError(f"failed to clone session to {target_path}: {exc}") from exc
    return target_path


def _clone_history(
    src_home: Path, dst_home: Path, old_id: str, new_id: str
) -> None:
    src_history = src_home / "history.jsonl"
    if not src_history.is_file():
        return
    additions: list[str] = []
    for line in src_history.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("session_id") == old_id:
            record["session_id"] = new_id
            additions.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    if additions:
        dst_home.mkdir(parents=True, exist_ok=True)
        with (dst_home / "history.jsonl").open("a", encoding="utf-8") as fh:
            for line in additions:
                fh.write(line + "\n")


def _clone_thread_state(
    src_home: Path,
    dst_home: Path,
    old_id: str,
    new_id: str,
    rollout_path: Path,
    provider: str,
) -> bool:
    src_database = (src_home / "state_5.sqlite").resolve()
    dst_database = (dst_home / "state_5.sqlite").resolve()
    # A profile that has never launched Codex does not have a database yet.
    # Codex creates it and backfills rollout files on first startup.
    if not src_database.is_file() or not dst_database.is_file():
        return False

    try:
        with sqlite3.connect(src_database) as source:
            source.row_factory = sqlite3.Row
            row = source.execute("SELECT * FROM threads WHERE id = ?", (old_id,)).fetchone()
            if row is None:
                return False
            values = dict(row)
            tools = source.execute(
                "SELECT position, name, description, input_schema, defer_loading, namespace "
                "FROM thread_dynamic_tools WHERE thread_id = ? ORDER BY position",
                (old_id,),
            ).fetchall()

        values["id"] = new_id
        values["rollout_path"] = str(rollout_path)
        values["model_provider"] = provider
        now_seconds = int(time.time())
        now_millis = int(time.time() * 1000)
        for key in ("created_at", "updated_at", "recency_at"):
            if key in values:
                values[key] = now_seconds
        for key in ("created_at_ms", "updated_at_ms", "recency_at_ms"):
            if key in values:
                values[key] = now_millis

        with sqlite3.connect(dst_database) as target:
            columns = [row[1] for row in target.execute("PRAGMA table_info(threads)")]
            insert_columns = [column for column in columns if column in values]
            placeholders = ",".join("?" for _ in insert_columns)
            names = ",".join(f'"{column}"' for column in insert_columns)
            target.execute(
                f"INSERT INTO threads ({names}) VALUES ({placeholders})",
                [values[column] for column in insert_columns],
            )
            for tool in tools:
                target.execute(
                    "INSERT INTO thread_dynamic_tools "
                    "(thread_id, position, name, description, input_schema, defer_loading, namespace) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (new_id, *tuple(tool)),
                )
        return True
    except sqlite3.Error as exc:
        raise SessionRepairError(f"failed to clone thread state: {exc}") from exc


def clone_session_for_profile(
    src_home: Path, session: SessionFile, dst_home: Path, provider: str
) -> SessionCloneResult:
    """Copy a session to a new ID and adapt only the copy for a provider."""
    new_id = str(uuid.uuid4())
    target_path = _clone_jsonl(session, dst_home, new_id, provider)
    try:
        _clone_thread_state(
            src_home, dst_home, session.session_id, new_id, target_path, provider
        )
        _clone_history(src_home, dst_home, session.session_id, new_id)
    except Exception:
        target_path.unlink(missing_ok=True)
        raise
    return SessionCloneResult(
        source_session_id=session.session_id,
        session_id=new_id,
        provider=provider,
        path=target_path,
        target_home=dst_home,
    )
