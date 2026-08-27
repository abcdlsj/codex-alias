"""Session migration orchestration across isolated Codex homes.

The JSONL/SQLite implementation remains in :mod:`codex_alias.sessions`.  This
service supplies the cross-home policy: source discovery, target configuration,
provider identity, and the public operations used by the CLI facade.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from .config import Config
from .errors import (
    AmbiguousSessionError,
    HomeNotFoundError,
    SessionNotFoundError,
    SessionRepairError,
)
from .home_service import safe_resolve
from .models import (
    Profile,
    DetectedSession,
    SessionCloneResult,
    SessionCopyResult,
    SessionFile,
    SessionFixResult,
)
from . import sessions as sessions_mod
from .session_mappings import SessionMappingContext


@dataclass(frozen=True, slots=True)
class _DetectionCandidate:
    """Internal candidate assembled from a thread row or rollout file."""

    session_id: str
    home: Path
    profile_hint: str | None
    rollout_path: Path | None
    score: float
    file_mtime: float
    preview: str | None = None


class SessionService:
    """Coordinate session operations without rendering or CLI concerns."""

    def __init__(
        self,
        config: Config,
        *,
        default_source_home: Callable[[], Path],
        profiles: Callable[[], Iterable[Profile]],
    ) -> None:
        self.config = config
        self._default_source_home = default_source_home
        self._profiles = profiles

    def list_sessions(self, home: Path) -> list[SessionFile]:
        return sessions_mod.list_session_files(home)

    def resolve_session(self, home: Path, query: str) -> SessionFile:
        return sessions_mod.resolve_session_file(home, query)

    def copy_session(
        self, src_home: Path, session: SessionFile, dst_home: Path
    ) -> SessionCopyResult:
        dst_home.mkdir(parents=True, exist_ok=True)
        return sessions_mod.copy_session(src_home, session, dst_home)

    def copy_session_by_query(
        self, src_home: Path, query: str, dst_home: Path
    ) -> SessionCopyResult:
        session = sessions_mod.resolve_session_file(src_home, query)
        dst_home.mkdir(parents=True, exist_ok=True)
        return sessions_mod.copy_session(src_home, session, dst_home)

    def copy_all_sessions(
        self, src_home: Path, dst_home: Path
    ) -> list[SessionCopyResult]:
        dst_home.mkdir(parents=True, exist_ok=True)
        return sessions_mod.copy_all_sessions(src_home, dst_home)

    def import_session(self, query: str, dst_home: Path) -> SessionCopyResult:
        """Copy one session from the canonical ``~/.codex`` home."""
        source = self._default_source_home()
        if not source.is_dir():
            raise HomeNotFoundError(f"default source home not found: {source}")
        return self.copy_session_by_query(source, query, dst_home)

    def find_session(self, query: str) -> tuple[Path, SessionFile]:
        """Find a session across the default and managed profile homes."""
        homes = [self._default_source_home(), *(p.path for p in self._profiles())]
        seen: set[Path] = set()
        matches: list[tuple[Path, SessionFile]] = []
        for home in homes:
            root = safe_resolve(home / "sessions")
            if root in seen or not root.is_dir():
                continue
            seen.add(root)
            try:
                matches.append((home, sessions_mod.resolve_session_file(home, query)))
            except (HomeNotFoundError, SessionNotFoundError):
                continue
        if not matches:
            raise SessionNotFoundError(f"session not found: {query}")
        unique = {safe_resolve(item[1].path): item for item in matches}
        if len(unique) > 1:
            raise AmbiguousSessionError(
                query, [str(item[1].path) for item in unique.values()]
            )
        return next(iter(unique.values()))

    def detect_last_session(self, cwd: Path | None = None) -> DetectedSession | None:
        """Find the latest session recorded for ``cwd`` across all profiles.

        Codex's thread index stores the rollout path, so an isolated profile
        can be identified without relying on the current ``CODEX_HOME``.  If
        profiles share the session store and that path points at the shared
        root, the current ``CODEX_HOME`` is used when it names a known profile;
        otherwise the result deliberately reports an unknown profile.
        """
        target_cwd = cwd if isinstance(cwd, Path) else sessions_mod._path_from_value(cwd)
        if target_cwd is None:
            target_cwd = Path.cwd()
        target_cwd = safe_resolve(target_cwd)

        homes = self._detection_homes()
        current_profile = self._current_profile_name(homes)
        candidates: list[_DetectionCandidate] = []
        for home, profile_hint in homes:
            candidates.extend(
                self._sqlite_detection_candidates(home, profile_hint, target_cwd)
            )

        # Older Codex homes may not have state_5.sqlite yet, and a partially
        # written thread row may not contain a rollout path.  Rollout metadata
        # is a safe, read-only fallback for both cases.
        if not candidates:
            unindexed_homes = [
                (home, profile_hint)
                for home, profile_hint in homes
                if self._sqlite_path(home) is None
            ]
            candidates.extend(
                self._rollout_detection_candidates(unindexed_homes, target_cwd)
            )
        candidates = self._dedupe_detection_candidates(candidates)
        if not candidates:
            return None

        candidate = max(
            candidates,
            key=lambda item: (item.score, item.file_mtime, item.session_id),
        )
        session_path = self._candidate_session_path(candidate, homes)
        if session_path is None:
            profile = self._infer_profile_name(
                candidate.rollout_path,
                candidate.profile_hint,
                current_profile,
                homes,
            )
            return DetectedSession(
                session_id=candidate.session_id,
                profile=profile,
                home=self._home_for_profile(profile, candidate.home, homes),
                path=candidate.rollout_path or candidate.home / "sessions",
                cwd=target_cwd,
                last_output=candidate.preview,
                updated_at=candidate.score or None,
            )

        profile = self._infer_profile_name(
            session_path,
            candidate.profile_hint,
            current_profile,
            homes,
        )
        metadata_cwd, metadata_timestamp, _ = sessions_mod.inspect_session_metadata(
            session_path
        )
        return DetectedSession(
            session_id=candidate.session_id,
            profile=profile,
            home=self._home_for_profile(profile, candidate.home, homes),
            path=session_path,
            cwd=safe_resolve(metadata_cwd or target_cwd),
            last_output=(
                sessions_mod.last_session_output(session_path)
                or candidate.preview
            ),
            updated_at=metadata_timestamp or candidate.score or None,
        )

    # ``detect_session`` reads naturally for library callers and keeps the
    # command implementation independent of the internal method name.
    def detect_session(self, cwd: Path | None = None) -> DetectedSession | None:
        return self.detect_last_session(cwd)

    def find_last_session(self, cwd: Path | None = None) -> DetectedSession | None:
        return self.detect_last_session(cwd)

    def _detection_homes(self) -> list[tuple[Path, str | None]]:
        homes: list[tuple[Path, str | None]] = []
        current = os.environ.get("CODEX_HOME")
        if current:
            homes.append((Path(current).expanduser(), None))
        homes.append((self._default_source_home(), "default"))
        homes.append((self.config.source_home, "default"))
        homes.extend((profile.path, profile.name) for profile in self._profiles())

        # Keep distinct raw paths so a shared store can still provide profile
        # hints, while avoiding exact duplicate entries from source/current.
        result: list[tuple[Path, str | None]] = []
        seen: set[tuple[Path, str | None]] = set()
        for home, profile_hint in homes:
            key = (home.expanduser(), profile_hint)
            if key in seen:
                continue
            seen.add(key)
            result.append((key[0], profile_hint))
        return result

    @staticmethod
    def _current_profile_name(
        homes: list[tuple[Path, str | None]],
    ) -> str | None:
        current = os.environ.get("CODEX_HOME")
        if not current:
            return None
        resolved = safe_resolve(Path(current).expanduser())
        for home, profile_hint in homes:
            if profile_hint and profile_hint != "default":
                if safe_resolve(home) == resolved:
                    return profile_hint
        return None

    @staticmethod
    def _same_cwd(value: object, target: Path) -> bool:
        path = sessions_mod._path_from_value(value)
        return path is not None and safe_resolve(path) == target

    @staticmethod
    def _sqlite_path(home: Path) -> Path | None:
        preferred = home / "state_5.sqlite"
        if preferred.is_file():
            return preferred
        # Be tolerant of future state database suffixes, but never inspect
        # numbered backups as if they were active state.
        for path in sorted(home.glob("state_*.sqlite")):
            if ".backup." not in path.name and path.is_file():
                return path
        return None

    def _sqlite_detection_candidates(
        self,
        home: Path,
        profile_hint: str | None,
        target_cwd: Path,
    ) -> list[_DetectionCandidate]:
        database = self._sqlite_path(home)
        if database is None:
            return []
        candidates: list[_DetectionCandidate] = []
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{database.resolve().as_uri()}?mode=ro",
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(threads)")
            }
            if "id" not in columns or "cwd" not in columns:
                return []
            rows = connection.execute("SELECT * FROM threads").fetchall()
            for row in rows:
                if not self._same_cwd(row["cwd"], target_cwd):
                    continue
                session_id = row["id"]
                if not isinstance(session_id, str) or not session_id.strip():
                    continue
                rollout_path = sessions_mod._path_from_value(
                    row["rollout_path"] if "rollout_path" in columns else None
                )
                if rollout_path is not None and not rollout_path.is_absolute():
                    rollout_path = home / rollout_path
                scores = [
                    sessions_mod._timestamp_value(row[column])
                    for column in (
                        "updated_at_ms",
                        "recency_at_ms",
                        "updated_at",
                        "recency_at",
                        "created_at_ms",
                        "created_at",
                    )
                    if column in columns
                ]
                score = max((value for value in scores if value is not None), default=0)
                file_mtime = self._mtime(rollout_path)
                candidates.append(
                    _DetectionCandidate(
                        session_id=session_id.strip(),
                        home=home,
                        profile_hint=profile_hint,
                        rollout_path=rollout_path,
                        score=score,
                        file_mtime=file_mtime,
                        preview=(
                            row["preview"].strip()
                            if "preview" in columns
                            and isinstance(row["preview"], str)
                            and row["preview"].strip()
                            else None
                        ),
                    )
                )
        except (OSError, sqlite3.Error):
            return []
        finally:
            if connection is not None:
                connection.close()
        return candidates

    def _rollout_detection_candidates(
        self,
        homes: list[tuple[Path, str | None]],
        target_cwd: Path,
    ) -> list[_DetectionCandidate]:
        candidates: list[_DetectionCandidate] = []
        for home, profile_hint in homes:
            for session in sessions_mod.list_session_files(home):
                metadata_cwd, timestamp, _ = sessions_mod.inspect_session_metadata(
                    session.path
                )
                if metadata_cwd is None or safe_resolve(metadata_cwd) != target_cwd:
                    continue
                mtime = self._mtime(session.path)
                candidates.append(
                    _DetectionCandidate(
                        session_id=session.session_id,
                        home=home,
                        profile_hint=profile_hint,
                        rollout_path=session.path,
                        score=timestamp or mtime,
                        file_mtime=mtime,
                    )
                )
        return candidates

    @staticmethod
    def _mtime(path: Path | None) -> float:
        if path is None:
            return 0
        try:
            return path.stat().st_mtime
        except OSError:
            return 0

    def _dedupe_detection_candidates(
        self,
        candidates: list[_DetectionCandidate],
    ) -> list[_DetectionCandidate]:
        by_key: dict[tuple[str, str], _DetectionCandidate] = {}
        for candidate in candidates:
            path = candidate.rollout_path
            if path is not None:
                key_path = str(safe_resolve(path)) if path.exists() else str(path)
            else:
                key_path = ""
            key = (candidate.session_id, key_path)
            existing = by_key.get(key)
            if existing is None or (
                candidate.score,
                candidate.file_mtime,
            ) > (existing.score, existing.file_mtime):
                by_key[key] = candidate
        return list(by_key.values())

    def _candidate_session_path(
        self,
        candidate: _DetectionCandidate,
        homes: list[tuple[Path, str | None]],
    ) -> Path | None:
        path = candidate.rollout_path
        if path is not None and path.is_file():
            return path
        search_homes = [candidate.home, *(home for home, _ in homes)]
        seen: set[Path] = set()
        for home in search_homes:
            resolved = safe_resolve(home / "sessions")
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                return sessions_mod.resolve_session_file(home, candidate.session_id).path
            except (HomeNotFoundError, SessionNotFoundError, AmbiguousSessionError):
                continue
        return None

    @staticmethod
    def _infer_profile_name(
        rollout_path: Path | None,
        profile_hint: str | None,
        current_profile: str | None,
        homes: list[tuple[Path, str | None]],
    ) -> str | None:
        if rollout_path is not None:
            raw = rollout_path.expanduser()
            resolved = safe_resolve(raw)
            # Prefer the literal path: shared stores preserve the profile path
            # in rollout_path even though the final file resolves to ~/.codex.
            for home, profile in homes:
                if profile and profile != "default":
                    profile_path = home.expanduser()
                    if raw == profile_path or profile_path in raw.parents:
                        return profile
            for home, profile in homes:
                if profile and profile != "default":
                    profile_resolved = safe_resolve(home)
                    if profile_resolved == resolved or profile_resolved in resolved.parents:
                        return profile
            for home, profile in homes:
                if profile == "default" and safe_resolve(home) == resolved:
                    return "default"
        if current_profile is not None:
            return current_profile
        return profile_hint

    @staticmethod
    def _home_for_profile(
        profile: str | None,
        fallback: Path,
        homes: list[tuple[Path, str | None]],
    ) -> Path:
        if profile == "default":
            for home, hint in homes:
                if hint == "default":
                    return home
        if profile is not None:
            for home, hint in homes:
                if hint == profile:
                    return home
        return fallback

    def clone_session_for_profile(
        self, query: str, target_home: Path, *, allow_lossy: bool = True
    ) -> SessionCloneResult:
        src_home, session = self.find_session(query)
        provider = sessions_mod.configured_model_provider_or_none(target_home)
        if provider is None:
            provider, _, _ = sessions_mod.inspect_session_source(session)
        if provider is None:
            raise SessionRepairError(
                f"session provider is missing in both {session.path} and "
                f"config: {target_home / 'config.toml'}"
            )
        model = sessions_mod.configured_model_or_none(target_home)
        mapping_context = self._session_mapping_context(
            session, src_home, target_home, provider, model
        )
        return sessions_mod.clone_session_for_profile(
            src_home,
            session,
            target_home,
            provider,
            model,
            mapping_context=mapping_context,
            allow_lossy=allow_lossy,
        )

    def configured_model_provider(self, home: Path) -> str:
        return sessions_mod.configured_model_provider(home)

    def configured_model(self, home: Path) -> str:
        return sessions_mod.configured_model(home)

    def fix_session_provider(
        self,
        home: Path,
        query: str,
        provider: str,
        *,
        model: str | None = None,
        from_provider: str | None = None,
        dry_run: bool = False,
        allow_lossy: bool = True,
    ) -> SessionFixResult:
        session = sessions_mod.resolve_session_file(home, query)
        mapping_context = self._session_mapping_context(
            session, home, home, provider, model
        )
        result = sessions_mod.fix_session_provider(
            session,
            provider,
            model=model,
            from_provider=from_provider,
            dry_run=dry_run,
            mapping_context=mapping_context,
            allow_lossy=allow_lossy,
        )
        state_changed, state_backup_path = sessions_mod.fix_session_state_provider(
            home,
            session.session_id,
            provider,
            model=model,
            from_provider=from_provider,
            dry_run=dry_run,
        )
        return replace(
            result,
            state_changed=state_changed,
            state_backup_path=state_backup_path,
        )

    def _backend_identity(
        self, provider: str | None, preferred_home: Path
    ) -> tuple[str, str | None] | None:
        """Resolve a provider alias without assuming the alias is the backend."""
        if provider is None:
            return None
        homes = [
            preferred_home,
            self._default_source_home(),
            self.config.source_home,
            *(profile.path for profile in self._profiles()),
        ]
        identities: set[tuple[str, str | None]] = set()
        seen: set[Path] = set()
        for home in homes:
            resolved = safe_resolve(home)
            if resolved in seen:
                continue
            seen.add(resolved)
            identity = sessions_mod.configured_backend_identity(home, provider)
            if identity is not None:
                identities.add(identity)
        if len(identities) == 1:
            return next(iter(identities))
        # Ambiguous or absent definitions are unknown. Guessing here could make
        # us discard valid ciphertext, so mappings remain conservative.
        return None

    def _session_mapping_context(
        self,
        session: SessionFile,
        source_home: Path,
        target_home: Path,
        target_provider: str,
        target_model: str | None,
    ) -> SessionMappingContext:
        source_provider, source_model, source_cli_version = (
            sessions_mod.inspect_session_source(session)
        )
        if target_model is None:
            try:
                target_model = sessions_mod.configured_model_or_none(target_home)
            except SessionRepairError:
                target_model = None
        source_identity = self._backend_identity(source_provider, source_home)
        target_identity = sessions_mod.configured_backend_identity(
            target_home, target_provider
        )
        return SessionMappingContext(
            source_model=source_model,
            target_model=target_model,
            source_provider=source_provider,
            target_provider=target_provider,
            source_backend_fingerprint=(
                source_identity[0] if source_identity is not None else None
            ),
            target_backend_fingerprint=(
                target_identity[0] if target_identity is not None else None
            ),
            source_cli_version=source_cli_version,
            target_wire_api=(
                target_identity[1] if target_identity is not None else None
            ),
        )
