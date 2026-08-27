"""High-level facade over profiles, homes, wrappers, and sessions.

``CodexAlias`` is the single object CLIs and other tools construct. Every method
is UI-free: it performs filesystem work and returns value objects or raises a
:class:`~codex_alias.errors.CodexAliasError`. Interactive selection/prompting lives
in the caller.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .models import (
    DoctorReport,
    HomeRef,
    HookOption,
    HookSyncResult,
    LinkAction,
    Profile,
    ProfileRemoveResult,
    SessionCopyResult,
    SessionCloneResult,
    SessionFile,
    SessionFixResult,
)
from . import hooks as hooks_mod
from . import profile_state
from .home_service import (
    REF_CURRENT,
    REF_SOURCE,
    HomeResolver,
    safe_resolve,
)
from .doctor_service import DoctorService
from .launcher import ProfileLauncher
from .profile_service import ProfileStore
from .session_service import SessionService
from .session_mappings import SessionMappingContext
from .sharing_service import SessionSharingService
from .validation import validate_name  # backwards-compatible package export


class CodexAlias:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.launcher = ProfileLauncher(config)
        self.profile_store = ProfileStore(config, self.launcher)
        self.home_resolver = HomeResolver(config, self.list_profiles)
        self.session_service = SessionService(
            config,
            default_source_home=self.default_source_home,
            profiles=self.list_profiles,
        )
        self.sharing_service = SessionSharingService(
            config,
            profile_home=self.profile_home,
            resolve_home_ref=self.resolve_home_ref,
        )
        self.doctor_service = DoctorService(config)

    # ------------------------------------------------------------------ homes

    def current_home(self) -> Path:
        """The home a bare ``codex`` invocation would use right now."""
        return self.home_resolver.current_home()

    def default_source_home(self) -> Path:
        """The canonical ``~/.codex`` used by :meth:`import_session`."""
        return self.home_resolver.default_source_home()

    def describe_home(self, path: Path) -> HomeRef:
        """Classify a home path relative to current/source/profiles."""
        return self.home_resolver.describe_home(path)

    def resolve_home_ref(self, ref: str | None) -> HomeRef:
        """Turn ``@source`` / ``@current`` / profile name / path into a home.

        Defaults to the current home when ``ref`` is empty.
        """
        return self.home_resolver.resolve_home_ref(ref)

    # --------------------------------------------------------------- profiles

    def list_profiles(self) -> list[Profile]:
        return self.profile_store.list_profiles()

    def profile_home(self, profile: str, *, must_exist: bool = False) -> Path:
        return self.profile_store.profile_home(profile, must_exist=must_exist)

    def add_profile(self, profile: str, command_name: str | None = None) -> Path:
        """Create a profile home and its wrapper command; return wrapper path."""
        return self.profile_store.add_profile(profile, command_name)

    def root_hooks_path(self) -> Path:
        """Return the hooks file belonging to the configured source home."""
        return hooks_mod.hooks_path(self.config.source_home)

    def profile_hook_options(self, profile: str) -> list[HookOption]:
        """List root hooks and their saved selection for PROFILE."""
        profile_path = self.profile_home(profile, must_exist=True)
        return hooks_mod.list_options(self.config.source_home, profile_path)

    def configure_profile_hooks(
        self, profile: str, selected: set[str]
    ) -> HookSyncResult:
        """Apply and remember selected root hooks for PROFILE."""
        profile_path = self.profile_home(profile, must_exist=True)
        return hooks_mod.configure_hooks(
            self.config.source_home, profile_path, selected
        )

    def record_profile_sync_type(self, profile: str, sync_type: str) -> None:
        """Remember that PROFILE should run one migration type during sync."""
        profile_path = self.profile_home(profile, must_exist=True)
        profile_state.record_sync_type(profile_path, sync_type)

    def remove_profile_sync_type(self, profile: str, sync_type: str) -> None:
        """Forget one saved migration type for PROFILE."""
        profile_path = self.profile_home(profile, must_exist=True)
        profile_state.remove_sync_type(profile_path, sync_type)

    def profile_sync_types(self, profile: str) -> tuple[str, ...]:
        """Return PROFILE's ordered migration types, if any were recorded."""
        profile_path = self.profile_home(profile, must_exist=True)
        return profile_state.saved_sync_types(profile_path)

    def profile_skill_sync_options(self, profile: str) -> dict[str, object] | None:
        """Return PROFILE's persisted skill selector, if one exists."""
        profile_path = self.profile_home(profile, must_exist=True)
        return profile_state.saved_skill_sync_options(profile_path)

    def record_profile_skill_sync_options(
        self,
        profile: str,
        *,
        include: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
        include_system: bool = False,
        prune: bool = False,
    ) -> None:
        """Persist PROFILE's skill selector and enable skill synchronization."""
        profile_path = self.profile_home(profile, must_exist=True)
        profile_state.record_skill_sync_options(
            profile_path,
            include=include,
            exclude=exclude,
            include_system=include_system,
            prune=prune,
        )

    def sync_profile_hooks(self, profile: str) -> HookSyncResult:
        """Reapply PROFILE's saved root-hook selection."""
        profile_path = self.profile_home(profile, must_exist=True)
        return hooks_mod.sync_saved_hooks(self.config.source_home, profile_path)

    def remove_wrapper(self, profile: str, command_name: str | None = None) -> tuple[Path, bool]:
        """Delete a wrapper command; profile data is left intact."""
        return self.profile_store.remove_wrapper(profile, command_name)

    def remove_profile(
        self,
        profile: str,
        command_name: str | None = None,
        *,
        keep_data: bool = False,
    ) -> ProfileRemoveResult:
        """Remove a profile: its wrapper command and, unless ``keep_data``, its home.

        Deleting a home is refused when it is the configured source home or the
        current ``CODEX_HOME``, since removing either would break the tool.
        """
        return self.profile_store.remove_profile(
            profile,
            command_name,
            keep_data=keep_data,
            source_home=self.config.source_home,
            current_home=self.current_home(),
        )

    def run_argv(self, profile: str, args: list[str]) -> tuple[list[str], dict[str, str]]:
        """Build the argv and environment to exec ``codex`` under ``profile``.

        Returns without executing so the caller controls process replacement.
        """
        return self.launcher.run_argv(profile, args)

    def resume_argv(
        self, home: Path, session_id: str, args: list[str] | None = None
    ) -> tuple[list[str], dict[str, str]]:
        """Build a resume invocation through the configured Codex wrapper."""
        return self.launcher.resume_argv(home, session_id, args)

    # Kept as compatibility shims for callers that used the old internals.
    def _codex_argv(self, args: list[str]) -> list[str]:
        return self.launcher.codex_argv(args)

    def _wrapper_script(self, profile: str) -> str:
        return self.launcher.wrapper_script(profile)

    def refresh_wrappers(self) -> list[Path]:
        """Regenerate default wrapper commands for every existing profile."""
        return self.profile_store.refresh_wrappers()

    # --------------------------------------------------------------- sessions

    def list_sessions(self, home: Path) -> list[SessionFile]:
        return self.session_service.list_sessions(home)

    def resolve_session(self, home: Path, query: str) -> SessionFile:
        return self.session_service.resolve_session(home, query)

    def copy_session(
        self, src_home: Path, session: SessionFile, dst_home: Path
    ) -> SessionCopyResult:
        return self.session_service.copy_session(src_home, session, dst_home)

    def copy_session_by_query(
        self, src_home: Path, query: str, dst_home: Path
    ) -> SessionCopyResult:
        return self.session_service.copy_session_by_query(src_home, query, dst_home)

    def copy_all_sessions(
        self, src_home: Path, dst_home: Path
    ) -> list[SessionCopyResult]:
        return self.session_service.copy_all_sessions(src_home, dst_home)

    def import_session(self, query: str, dst_home: Path) -> SessionCopyResult:
        """Copy one session from the default ``~/.codex`` into ``dst_home``."""
        return self.session_service.import_session(query, dst_home)

    def find_session(self, query: str) -> tuple[Path, SessionFile]:
        """Find a session across default and profile homes, de-duplicating links."""
        return self.session_service.find_session(query)

    def _backend_identity(
        self, provider: str | None, preferred_home: Path
    ) -> tuple[str, str | None] | None:
        return self.session_service._backend_identity(provider, preferred_home)

    def _session_mapping_context(
        self,
        session: SessionFile,
        source_home: Path,
        target_home: Path,
        target_provider: str,
        target_model: str | None,
    ) -> SessionMappingContext:
        return self.session_service._session_mapping_context(
            session,
            source_home,
            target_home,
            target_provider,
            target_model,
        )

    def clone_session_for_profile(
        self, query: str, target_home: Path, *, allow_lossy: bool = True
    ) -> SessionCloneResult:
        return self.session_service.clone_session_for_profile(
            query, target_home, allow_lossy=allow_lossy
        )

    def configured_model_provider(self, home: Path) -> str:
        return self.session_service.configured_model_provider(home)

    def configured_model(self, home: Path) -> str:
        return self.session_service.configured_model(home)

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
        return self.session_service.fix_session_provider(
            home,
            query,
            provider,
            model=model,
            from_provider=from_provider,
            dry_run=dry_run,
            allow_lossy=allow_lossy,
        )

    def candidate_source_homes(self, target_home: Path) -> list[HomeRef]:
        """Source homes usable for migration into ``target_home`` (excludes it)."""
        return self.home_resolver.candidate_source_homes(target_home)

    # ---------------------------------------------------------------- sharing

    def share_sessions(self, profile: str, source_ref: str = REF_SOURCE) -> list[LinkAction]:
        """Symlink a profile's sessions/history/db to a source home.

        Non-destructive: existing real files are backed up (``.backup.<n>``)
        before a symlink replaces them, and identical symlinks are left alone.
        """
        return self.sharing_service.share_sessions(profile, source_ref)

    def link_shared(self, profile_path: Path, source_home: Path) -> list[LinkAction]:
        """Link session artifacts without changing the profile sync state."""
        return self.sharing_service.link_shared(profile_path, source_home)

    # Kept as a compatibility shim for callers that used the old internal
    # helper. New code should use :meth:`link_shared` or :meth:`share_sessions`.
    def _link_shared(self, profile_path: Path, source_home: Path) -> list[LinkAction]:
        return self.link_shared(profile_path, source_home)

    # ----------------------------------------------------------------- doctor

    def doctor(self) -> DoctorReport:
        return self.doctor_service.report()

    # ------------------------------------------------------------- internals

    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        return safe_resolve(path)

    def _resolve_existing(self, path: Path) -> Path:
        return self.home_resolver._resolve_existing(path)

    @staticmethod
    def _force_symlink(source: Path, link: Path) -> None:
        SessionSharingService._force_symlink(source, link)

    def _backup(self, path: Path) -> Path:
        """Move ``path`` aside to a unique ``.backup.N`` name and return it."""
        return SessionSharingService._backup(path)
