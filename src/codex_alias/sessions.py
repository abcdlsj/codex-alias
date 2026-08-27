"""Session discovery and migration between Codex homes.

A Codex home stores conversations as ``sessions/**/*.jsonl`` plus a top-level
``history.jsonl`` index. This module reads and copies those artifacts without
any user interaction; the CLI drives selection/prompting on top.
"""

from __future__ import annotations

import copy
import filecmp
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib

from .errors import (
    AmbiguousSessionError,
    HomeNotFoundError,
    SessionConflictError,
    SessionLossyMappingError,
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
from .session_mappings import (
    SessionMappingContext,
    SessionMappingResult,
    apply_session_mappings,
)

_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def _split_jsonl(text: str, *, keepends: bool = False) -> list[str]:
    """Split JSONL only at LF, so Unicode line separators stay in strings."""
    parts = text.split("\n")
    has_trailing_lf = parts[-1] == ""
    if has_trailing_lf:
        parts.pop()

    if not keepends:
        return [part[:-1] if part.endswith("\r") else part for part in parts]
    if not parts:
        return []
    if has_trailing_lf:
        return [f"{part}\n" for part in parts]
    return [f"{part}\n" for part in parts[:-1]] + [parts[-1]]


def _normalize_paginated_ordinals(records: list[dict[str, object]]) -> None:
    """Restore the contiguous ordinal invariant of paginated rollouts.

    Codex uses the top-level ``ordinal`` as the cursor for paginated history.
    Any mapping that removes a record must therefore renumber the remaining
    records before they are persisted.  Non-paginated and legacy rollouts are
    left untouched because they do not use this cursor contract.
    """
    paginated = False
    for record in records:
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        if isinstance(payload, dict) and payload.get("history_mode") == "paginated":
            paginated = True
            break
    if not paginated:
        return
    for ordinal, record in enumerate(records):
        if record.get("ordinal") != ordinal:
            record["ordinal"] = ordinal


def _rewrite_session_references(
    record: dict[str, object], old_id: str, new_id: str
) -> None:
    """Rewrite only the session/thread fields in their Codex record positions."""
    record_type = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return

    if record_type == "session_meta":
        for key in ("id", "session_id"):
            if payload.get(key) == old_id:
                payload[key] = new_id
    elif record_type == "event_msg" and payload.get("thread_id") == old_id:
        payload["thread_id"] = new_id


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


def _timestamp_value(value: object) -> float | None:
    """Normalize the timestamp forms used by Codex's SQLite/JSON stores."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # SQLite stores both seconds and milliseconds depending on the column.
        return number / 1000 if number > 10_000_000_000 else number
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return _timestamp_value(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


def _path_from_value(value: object) -> Path | None:
    """Turn a stored path (including a ``file://`` URI) into a Path."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.startswith("file://"):
        parsed = urlsplit(text)
        text = parsed.path
    if not text:
        return None
    return Path(text).expanduser()


def inspect_session_metadata(
    path: Path,
) -> tuple[Path | None, float | None, str | None]:
    """Read a rollout's working directory, timestamp, and provider.

    Detection is deliberately tolerant: an incomplete or partially-written
    rollout is ignored by callers instead of making the diagnostic command
    fail.  Full session migration continues to use the stricter validators.
    """
    cwd: Path | None = None
    timestamp: float | None = None
    provider: str | None = None
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(record, dict):
                    continue
                record_timestamp = _timestamp_value(record.get("timestamp"))
                if record_timestamp is not None:
                    timestamp = record_timestamp
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if cwd is None:
                    cwd = _path_from_value(payload.get("cwd"))
                value = payload.get("model_provider")
                if isinstance(value, str) and value.strip():
                    provider = value.strip()
                if timestamp is None:
                    timestamp = _timestamp_value(payload.get("timestamp"))
                # session_meta is normally the first record; once all useful
                # fields are present there is no reason to scan the rest.
                if cwd is not None and timestamp is not None:
                    break
    except (OSError, UnicodeError):
        return None, None, None
    if timestamp is None:
        try:
            timestamp = path.stat().st_mtime
        except OSError:
            timestamp = None
    return cwd, timestamp, provider


def _message_text(value: object) -> str | None:
    """Extract output text from a message/content value."""
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, dict):
        for key in ("text", "message", "last_agent_message", "content"):
            candidate = _message_text(value.get(key))
            if candidate is not None:
                return candidate
        return None
    if not isinstance(value, list):
        return None
    chunks: list[str] = []
    for item in value:
        if isinstance(item, str):
            if item:
                chunks.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type is not None and item_type not in {
            "output_text",
            "text",
            "assistant_message",
            "message",
        }:
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            chunks.append(text)
    text = "\n".join(chunks)
    return text if text.strip() else None


def last_session_output(path: Path) -> str | None:
    """Return the last user-visible assistant output in a rollout.

    Codex has used both ``response_item`` assistant messages and
    ``event_msg/task_complete.last_agent_message`` over its releases.  Keep
    reasoning, tool calls, and encrypted content out of this view.
    """
    latest: str | None = None
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                record_type = record.get("type")
                payload_type = payload.get("type")

                candidate: str | None = None
                if record_type == "response_item":
                    if payload_type == "message":
                        role = payload.get("role")
                        if role in (None, "assistant"):
                            candidate = _message_text(payload.get("content"))
                    elif payload_type in {
                        "output_text",
                        "assistant_message",
                    }:
                        candidate = _message_text(
                            payload.get("text") or payload.get("message")
                        )
                elif record_type == "event_msg":
                    if payload_type in {
                        "task_complete",
                        "turn_complete",
                        "agent_message",
                        "assistant_message",
                    }:
                        candidate = _message_text(
                            payload.get("last_agent_message")
                            or payload.get("message")
                            or payload.get("text")
                        )
                    elif payload_type == "item_completed":
                        item = payload.get("item")
                        if isinstance(item, dict) and item.get("type") in {
                            "AgentMessage",
                            "agent_message",
                            "assistant_message",
                            "message",
                        }:
                            candidate = _message_text(
                                item.get("text")
                                or item.get("message")
                                or item.get("content")
                            )
                if candidate is not None:
                    latest = candidate.strip()
    except (OSError, UnicodeError):
        return None
    return latest


def _append_history(src_home: Path, dst_home: Path, session_id: str) -> None:
    """Copy this session's history lines into the target, de-duplicated."""
    src_history = src_home / "history.jsonl"
    if not src_history.is_file():
        return

    needle = f'"session_id":"{session_id}"'
    new_lines = [
        line
        for line in _split_jsonl(src_history.read_text(encoding="utf-8"))
        if line and needle in line
    ]
    if not new_lines:
        return

    dst_home.mkdir(parents=True, exist_ok=True)
    dst_history = dst_home / "history.jsonl"
    existing = set()
    if dst_history.is_file():
        existing = set(_split_jsonl(dst_history.read_text(encoding="utf-8")))

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


def configured_model_provider_or_none(home: Path) -> str | None:
    """Read the configured provider, allowing Codex's built-in default."""
    config_path = home / "config.toml"
    data = _read_config(home)
    provider = data.get("model_provider")
    if provider is None:
        return None
    if not isinstance(provider, str) or not provider.strip():
        raise SessionRepairError(
            f"invalid top-level model_provider in config: {config_path}"
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


def configured_model_or_none(home: Path) -> str | None:
    """Read the configured model, allowing Codex to use its default."""
    config_path = home / "config.toml"
    data = _read_config(home)
    model = data.get("model")
    if model is None:
        return None
    if not isinstance(model, str) or not model.strip():
        raise SessionRepairError(
            f"invalid top-level model in config: {config_path}"
        )
    return model.strip()


def configured_backend_identity(
    home: Path, provider: str
) -> tuple[str, str | None] | None:
    """Return a stable wire-API/base-URL identity for a provider alias.

    Provider names are intentionally excluded: two aliases pointing at the
    same URL and wire API are the same encryption boundary for our purposes.
    Missing definitions stay unknown instead of being guessed.
    """
    try:
        data = _read_config(home)
    except SessionRepairError:
        return None
    providers = data.get("model_providers")
    if not isinstance(providers, dict):
        return None
    definition = providers.get(provider)
    if not isinstance(definition, dict):
        return None
    base_url = definition.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    wire_api = definition.get("wire_api", "responses")
    if not isinstance(wire_api, str) or not wire_api.strip():
        return None

    parts = urlsplit(base_url.strip())
    if not parts.scheme or not parts.netloc:
        return None
    normalized_url = urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path.rstrip("/"),
            parts.query,
            "",
        )
    )
    normalized_wire = wire_api.strip().casefold()
    return f"{normalized_wire}|{normalized_url}", normalized_wire


def inspect_session_source(
    session: SessionFile,
) -> tuple[str | None, str | None, str | None]:
    """Read source provider, latest model, and CLI version from a rollout."""
    provider: str | None = None
    model: str | None = None
    cli_version: str | None = None
    try:
        lines = _split_jsonl(session.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise SessionRepairError(f"failed to read session {session.path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SessionRepairError(
                f"invalid JSONL record at {session.path}:{line_number}: {exc.msg}"
            ) from exc
        if not isinstance(record, dict):
            raise SessionRepairError(
                f"invalid non-object JSONL record at {session.path}:{line_number}"
            )
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "session_meta":
            value = payload.get("model_provider")
            if isinstance(value, str) and value.strip():
                provider = value.strip()
            value = payload.get("cli_version")
            if isinstance(value, str) and value.strip():
                cli_version = value.strip()
        if record.get("type") == "turn_context":
            value = payload.get("model")
            if isinstance(value, str) and value.strip():
                model = value.strip()
        if record.get("type") == "event_msg":
            settings = payload.get("thread_settings")
            if isinstance(settings, dict):
                value = settings.get("model")
                if isinstance(value, str) and value.strip():
                    model = value.strip()
    return provider, model, cli_version


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
    mapping_context: SessionMappingContext | None = None,
    allow_lossy: bool = True,
) -> SessionFixResult:
    """Normalize persisted provider and model metadata in a Codex session.

    Every JSONL record is parsed before any write occurs. On a real repair the
    original is copied to a unique sibling backup and the replacement is
    written atomically. Only the two provider fields used by Codex session
    bootstrap and the thread model are changed. Registered provider mappings
    may also normalize response items that the target model/API cannot replay.
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
        original_lines = _split_jsonl(
            path.read_text(encoding="utf-8"), keepends=True
        )
    except (OSError, UnicodeError) as exc:
        raise SessionRepairError(f"failed to read session {session.path}: {exc}") from exc

    records: list[dict[str, object]] = []
    newlines: list[str] = []
    original_records: list[dict[str, object]] = []
    previous: set[str] = set()
    previous_models: set[str] = set()
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

        original_records.append(copy.deepcopy(record))
        newlines.append(newline)
        payload = record.get("payload")
        if record.get("type") == "session_meta":
            changed_fields += int(
                _replace_provider_field(
                    payload, "model_provider", provider, from_provider, previous
                )
            )
        if record.get("type") == "event_msg" and isinstance(payload, dict):
            changed_fields += int(
                _replace_provider_field(
                    payload.get("thread_settings"),
                    "model_provider_id",
                    provider,
                    from_provider,
                    previous,
                )
            )
            if model is not None:
                changed_model_fields += int(
                    _replace_model_field(
                        payload.get("thread_settings"), model, previous_models
                    )
                )
        records.append(record)

    if mapping_context is None:
        mapping_context = SessionMappingContext(
            source_model=None,
            target_model=model,
            source_provider=None,
            target_provider=provider,
        )
    mapping_result = apply_session_mappings(records, mapping_context)
    if mapping_result.blockers:
        details = "; ".join(mapping_result.blockers)
        raise SessionRepairError(f"session mapping blocked: {details}")
    if mapping_result.lossy_mappings and not allow_lossy:
        raise SessionLossyMappingError(
            mapping_result.lossy_mappings,
            mapping_result.mapped_records,
            mapping_result.dropped_records,
        )
    _normalize_paginated_ordinals(list(mapping_result.records))

    kept_ids = {id(record) for record in mapping_result.records}
    changed_records = 0
    rewritten: list[str] = []
    for record, original, line, newline in zip(
        records, original_records, original_lines, newlines, strict=True
    ):
        if id(record) not in kept_ids:
            changed_records += 1
            continue
        if record != original:
            changed_records += 1
            rewritten.append(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + newline
            )
        else:
            rewritten.append(line)

    backup_path: Path | None = None
    if changed_records and not dry_run:
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
        mapped_records=mapping_result.mapped_records,
        applied_mappings=mapping_result.applied_mappings,
        dropped_records=mapping_result.dropped_records,
        lossy_mappings=mapping_result.lossy_mappings,
        mapping_warnings=mapping_result.warnings,
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
    session: SessionFile,
    dst_home: Path,
    new_id: str,
    provider: str,
    model: str | None,
    mapping_context: SessionMappingContext | None = None,
    allow_lossy: bool = True,
) -> tuple[Path, SessionMappingResult]:
    """Create a validated session copy with a new identity and provider."""
    try:
        lines = _split_jsonl(
            session.path.read_text(encoding="utf-8"), keepends=True
        )
    except (OSError, UnicodeError) as exc:
        raise SessionRepairError(f"failed to read session {session.path}: {exc}") from exc

    records: list[dict[str, object]] = []
    newlines: list[str] = []
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
        _rewrite_session_references(record, session.session_id, new_id)
        payload = record.get("payload")
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            payload["model_provider"] = provider
        if record.get("type") == "event_msg" and isinstance(payload, dict):
            settings = payload.get("thread_settings")
            if isinstance(settings, dict) and "model_provider_id" in settings:
                settings["model_provider_id"] = provider
        records.append(record)
        newlines.append(newline)

    if mapping_context is None:
        mapping_context = SessionMappingContext(
            source_model=None,
            target_model=model,
            source_provider=None,
            target_provider=provider,
        )
    mapping_result = apply_session_mappings(records, mapping_context)
    if mapping_result.blockers:
        details = "; ".join(mapping_result.blockers)
        raise SessionRepairError(f"session mapping blocked: {details}")
    if mapping_result.lossy_mappings and not allow_lossy:
        raise SessionLossyMappingError(
            mapping_result.lossy_mappings,
            mapping_result.mapped_records,
            mapping_result.dropped_records,
        )
    _normalize_paginated_ordinals(list(mapping_result.records))
    newline_by_id = {
        id(record): newline for record, newline in zip(records, newlines, strict=True)
    }
    rewritten = [
        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        + newline_by_id[id(record)]
        for record in mapping_result.records
    ]

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
    return target_path, mapping_result


def _clone_history(
    src_home: Path, dst_home: Path, old_id: str, new_id: str
) -> None:
    src_history = src_home / "history.jsonl"
    if not src_history.is_file():
        return
    additions: list[str] = []
    for line in _split_jsonl(src_history.read_text(encoding="utf-8")):
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
    src_home: Path,
    session: SessionFile,
    dst_home: Path,
    provider: str,
    model: str | None = None,
    mapping_context: SessionMappingContext | None = None,
    allow_lossy: bool = True,
) -> SessionCloneResult:
    """Copy a session to a new ID and adapt only the copy for a provider."""
    new_id = str(uuid.uuid4())
    target_path, mapping_result = _clone_jsonl(
        session,
        dst_home,
        new_id,
        provider,
        model,
        mapping_context,
        allow_lossy,
    )
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
        model=model,
        mapped_records=mapping_result.mapped_records,
        applied_mappings=mapping_result.applied_mappings,
        dropped_records=mapping_result.dropped_records,
        lossy_mappings=mapping_result.lossy_mappings,
        mapping_warnings=mapping_result.warnings,
    )
