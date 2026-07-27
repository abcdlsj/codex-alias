"""High-level facade over profiles, homes, wrappers, and sessions.

``CodexAlias`` is the single object CLIs and other tools construct. Every method
is UI-free: it performs filesystem work and returns value objects or raises a
:class:`~codex_alias.errors.CodexmError`. Interactive selection/prompting lives
in the caller.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import replace
from pathlib import Path

from .config import Config
from .errors import HomeNotFoundError, InvalidNameError, ProfileNotFoundError
from .models import (
    DoctorReport,
    HomeKind,
    HomeRef,
    LinkAction,
    Profile,
    SessionCopyResult,
    SessionFile,
    SessionFixResult,
)
from . import sessions as sessions_mod

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SHARED_DBS = ("state_5.sqlite", "logs_1.sqlite")

# Reference tokens accepted anywhere a home/profile is expected.
REF_SOURCE = "@source"
REF_CURRENT = "@current"


def validate_name(value: str, label: str) -> str:
    """Guard against path traversal and shell-hostile names."""
    if not value or not _NAME_RE.match(value):
        raise InvalidNameError(
            f"invalid {label} {value!r}. Allowed: letters, numbers, dot, "
            "underscore, dash."
        )
    return value


class CodexAlias:
    def __init__(self, config: Config) -> None:
        self.config = config

    # ------------------------------------------------------------------ homes

    def current_home(self) -> Path:
        """The home a bare ``codex`` invocation would use right now."""
        env_home = os.environ.get("CODEX_HOME")
        return Path(env_home) if env_home else self.config.source_home

    def default_source_home(self) -> Path:
        """The canonical ``~/.codex`` used by :meth:`import_session`."""
        return Path(os.environ.get("HOME", str(Path.home()))) / ".codex"

    def describe_home(self, path: Path) -> HomeRef:
        """Classify a home path relative to current/source/profiles."""
        resolved = self._resolve_existing(path)

        if resolved == self._safe_resolve(self.current_home()):
            return HomeRef(resolved, HomeKind.CURRENT)
        if resolved == self._safe_resolve(self.config.source_home):
            return HomeRef(resolved, HomeKind.SOURCE)
        for profile in self.list_profiles():
            if resolved == self._safe_resolve(profile.path):
                return HomeRef(resolved, HomeKind.PROFILE, profile.name)
        return HomeRef(resolved, HomeKind.OTHER)

    def resolve_home_ref(self, ref: str | None) -> HomeRef:
        """Turn ``@source`` / ``@current`` / profile name / path into a home.

        Defaults to the current home when ``ref`` is empty.
        """
        ref = ref or REF_CURRENT
        if ref == REF_SOURCE:
            return self.describe_home(self.config.source_home)
        if ref == REF_CURRENT:
            return self.describe_home(self.current_home())

        candidate = self.config.profile_path(ref)
        if candidate.is_dir():
            return self.describe_home(candidate)
        as_path = Path(ref).expanduser()
        if as_path.is_dir():
            return self.describe_home(as_path)
        raise HomeNotFoundError(f"unknown home/profile: {ref}")

    # --------------------------------------------------------------- profiles

    def list_profiles(self) -> list[Profile]:
        root = self.config.profile_root
        if not root.is_dir():
            return []
        out: list[Profile] = []
        for path in sorted(p for p in root.iterdir() if p.is_dir()):
            out.append(
                Profile(
                    name=path.name,
                    path=path,
                    sessions_shared=(path / "sessions").is_symlink(),
                )
            )
        return out

    def profile_home(self, profile: str, *, must_exist: bool = False) -> Path:
        validate_name(profile, "profile")
        path = self.config.profile_path(profile)
        if must_exist and not path.is_dir():
            raise ProfileNotFoundError(f"profile not found: {path}")
        return path

    def add_profile(self, profile: str, command_name: str | None = None) -> Path:
        """Create a profile home and its wrapper command; return wrapper path."""
        validate_name(profile, "profile")
        command_name = command_name or f"codex-{profile}"
        validate_name(command_name, "command name")

        profile_path = self.config.profile_path(profile)
        profile_path.mkdir(parents=True, exist_ok=True)
        self.config.bin_dir.mkdir(parents=True, exist_ok=True)

        target = self.config.wrapper_path(command_name)
        target.write_text(self._wrapper_script(profile), encoding="utf-8")
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return target

    def remove_wrapper(self, profile: str, command_name: str | None = None) -> tuple[Path, bool]:
        """Delete a wrapper command; profile data is left intact."""
        validate_name(profile, "profile")
        command_name = command_name or f"codex-{profile}"
        validate_name(command_name, "command name")
        target = self.config.wrapper_path(command_name)
        if target.exists():
            target.unlink()
            return target, True
        return target, False

    def run_argv(self, profile: str, args: list[str]) -> tuple[list[str], dict[str, str]]:
        """Build the argv and environment to exec ``codex`` under ``profile``.

        Returns without executing so the caller controls process replacement.
        """
        validate_name(profile, "profile")
        profile_path = self.config.profile_path(profile)
        profile_path.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["CODEX_HOME"] = str(profile_path)
        return [self.config.codex_cmd, *args], env

    def _wrapper_script(self, profile: str) -> str:
        return (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'PROFILE_ROOT="${{CODEXSWITCH_PROFILE_ROOT:-{self.config.profile_root}}}"\n'
            f'CODEX_CMD="${{CODEXSWITCH_CODEX_CMD:-{self.config.codex_cmd}}}"\n'
            f'export CODEX_HOME="${{PROFILE_ROOT}}/{profile}"\n'
            'mkdir -p "${CODEX_HOME}"\n'
            'exec "${CODEX_CMD}" "$@"\n'
        )

    # --------------------------------------------------------------- sessions

    def list_sessions(self, home: Path) -> list[SessionFile]:
        return sessions_mod.list_session_files(home)

    def resolve_session(self, home: Path, query: str) -> SessionFile:
        return sessions_mod.resolve_session_file(home, query)

    def copy_session(self, src_home: Path, session: SessionFile, dst_home: Path) -> SessionCopyResult:
        dst_home.mkdir(parents=True, exist_ok=True)
        return sessions_mod.copy_session(src_home, session, dst_home)

    def copy_session_by_query(self, src_home: Path, query: str, dst_home: Path) -> SessionCopyResult:
        session = sessions_mod.resolve_session_file(src_home, query)
        dst_home.mkdir(parents=True, exist_ok=True)
        return sessions_mod.copy_session(src_home, session, dst_home)

    def copy_all_sessions(self, src_home: Path, dst_home: Path) -> list[SessionCopyResult]:
        dst_home.mkdir(parents=True, exist_ok=True)
        return sessions_mod.copy_all_sessions(src_home, dst_home)

    def import_session(self, query: str, dst_home: Path) -> SessionCopyResult:
        """Copy one session from the default ``~/.codex`` into ``dst_home``."""
        source = self.default_source_home()
        if not source.is_dir():
            raise HomeNotFoundError(f"default source home not found: {source}")
        return self.copy_session_by_query(source, query, dst_home)

    def configured_model_provider(self, home: Path) -> str:
        return sessions_mod.configured_model_provider(home)

    def fix_session_provider(
        self,
        home: Path,
        query: str,
        provider: str,
        *,
        from_provider: str | None = None,
        dry_run: bool = False,
    ) -> SessionFixResult:
        session = sessions_mod.resolve_session_file(home, query)
        result = sessions_mod.fix_session_provider(
            session,
            provider,
            from_provider=from_provider,
            dry_run=dry_run,
        )
        state_changed, state_backup_path = sessions_mod.fix_session_state_provider(
            home,
            session.session_id,
            provider,
            from_provider=from_provider,
            dry_run=dry_run,
        )
        return replace(
            result,
            state_changed=state_changed,
            state_backup_path=state_backup_path,
        )

    def candidate_source_homes(self, target_home: Path) -> list[HomeRef]:
        """Source homes usable for migration into ``target_home`` (excludes it)."""
        target = self._safe_resolve(target_home)
        source = self._safe_resolve(self.config.source_home)
        refs: list[HomeRef] = []
        if source != target:
            refs.append(self.describe_home(self.config.source_home))
        for profile in self.list_profiles():
            resolved = self._safe_resolve(profile.path)
            if resolved not in (target, source):
                refs.append(HomeRef(resolved, HomeKind.PROFILE, profile.name))
        return refs

    # ---------------------------------------------------------------- sharing

    def share_sessions(self, profile: str, source_ref: str = REF_SOURCE) -> list[LinkAction]:
        """Symlink a profile's sessions/history/db to a source home.

        Non-destructive: existing real files are backed up (``.backup.<n>``)
        before a symlink replaces them, and identical symlinks are left alone.
        """
        profile_path = self.profile_home(profile, must_exist=True)
        source_home = self.resolve_home_ref(source_ref).path
        return self._link_shared(profile_path, source_home)

    def _link_shared(self, profile_path: Path, source_home: Path) -> list[LinkAction]:
        actions: list[LinkAction] = []
        sessions_link = profile_path / "sessions"
        source_sessions = source_home / "sessions"

        if sessions_link.exists() and not sessions_link.is_symlink():
            backup = self._backup(sessions_link)
            actions.append(LinkAction(f"Backed up existing sessions -> {backup}"))

        source_sessions.mkdir(parents=True, exist_ok=True)
        self._force_symlink(source_sessions, sessions_link)
        actions.append(LinkAction(f"Linked {sessions_link} -> {source_sessions}"))

        source_history = source_home / "history.jsonl"
        history_link = profile_path / "history.jsonl"
        if source_history.exists() or source_history.is_symlink():
            if history_link.exists() and not history_link.is_symlink():
                backup = self._backup(history_link)
                actions.append(LinkAction(f"Backed up existing history -> {backup}"))
            self._force_symlink(source_history, history_link)
            actions.append(LinkAction(f"Linked {history_link} -> {source_history}"))

        for db_name in _SHARED_DBS:
            source_db = source_home / db_name
            if not source_db.is_file():
                continue
            target_db = profile_path / db_name
            if target_db.exists() and not target_db.is_symlink():
                self._backup(target_db)
                for suffix in ("-wal", "-shm"):
                    sidecar = target_db.with_name(target_db.name + suffix)
                    sidecar.unlink(missing_ok=True)
                actions.append(LinkAction(f"Backed up existing {db_name}"))
            self._force_symlink(source_db, target_db)
            actions.append(LinkAction(f"Linked {target_db} -> {source_db}"))

        return actions

    # ----------------------------------------------------------------- doctor

    def doctor(self) -> DoctorReport:
        bin_dir = str(self.config.bin_dir)
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        codex_path = shutil.which(self.config.codex_cmd)
        return DoctorReport(
            codex_cmd=self.config.codex_cmd,
            source_home=self.config.source_home,
            profile_root=self.config.profile_root,
            bin_dir=self.config.bin_dir,
            manager_bin_name=self.config.manager_bin_name,
            bin_on_path=bin_dir in path_entries,
            codex_path=codex_path,
        )

    # ------------------------------------------------------------- internals

    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path.absolute()

    def _resolve_existing(self, path: Path) -> Path:
        if not path.exists():
            raise HomeNotFoundError(f"directory not found: {path}")
        return self._safe_resolve(path)

    @staticmethod
    def _force_symlink(source: Path, link: Path) -> None:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(source)

    def _backup(self, path: Path) -> Path:
        """Move ``path`` aside to a unique ``.backup.N`` name and return it."""
        n = 1
        while True:
            backup = path.with_name(f"{path.name}.backup.{n}")
            if not backup.exists():
                break
            n += 1
        shutil.move(str(path), str(backup))
        return backup
