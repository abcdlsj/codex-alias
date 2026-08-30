"""Synchronisation orchestration between the CLI and pure file operations.

``syncing`` contains the filesystem primitives.  This module owns the saved
sync plan, migration ordering, and callback boundaries for the few operations
that still need interactive rendering (hooks and session migration).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import syncing
from .profile_state import LEGACY_BUNDLE_TYPES

SyncMessage = syncing.SyncMessage
SYNC_TYPE_DESCRIPTIONS = syncing.SYNC_TYPE_DESCRIPTIONS
SYNC_TYPE_CHOICES = syncing.SYNC_TYPE_CHOICES
SYNC_CONFIRM_TYPES = syncing.SYNC_CONFIRM_TYPES

SyncEmitter = Callable[[str, str], None]
SyncConfirm = Callable[[str], bool]
SyncMigration = Callable[..., None]
SyncSkillCopy = Callable[..., None]
SyncHookRenderer = Callable[[Any], None]
SyncSessionMigration = Callable[[Any, Path], None]


def copy_resource_dirs(
    src: Path,
    dst: Path,
    names: tuple[str, ...],
    *,
    dry_run: bool = False,
    label: str = "resource",
) -> tuple[syncing.SyncMessage, ...]:
    return syncing.copy_resource_dirs(src, dst, names, dry_run=dry_run, label=label)


def copy_plugin_dirs(
    src: Path, dst: Path, *, dry_run: bool = False
) -> tuple[syncing.SyncMessage, ...]:
    return syncing.copy_plugin_dirs(src, dst, dry_run=dry_run)


def copy_plugin_only_dirs(
    src: Path, dst: Path, *, dry_run: bool = False
) -> tuple[syncing.SyncMessage, ...]:
    return syncing.copy_plugin_only_dirs(src, dst, dry_run=dry_run)


def copy_agents_dirs(
    src: Path, dst: Path, *, dry_run: bool = False
) -> tuple[syncing.SyncMessage, ...]:
    return syncing.copy_agents_dirs(src, dst, dry_run=dry_run)


def copy_mcp_dirs(
    src: Path, dst: Path, *, dry_run: bool = False
) -> tuple[syncing.SyncMessage, ...]:
    return syncing.copy_mcp_dirs(src, dst, dry_run=dry_run)


def copy_instruction_files(
    src: Path, dst: Path, *, dry_run: bool = False
) -> tuple[syncing.SyncMessage, ...]:
    return syncing.copy_instruction_files(src, dst, dry_run=dry_run)


def copy_core_config(
    src: Path, dst: Path, *, dry_run: bool = False
) -> tuple[syncing.SyncMessage, ...]:
    return syncing.copy_core_config(src, dst, dry_run=dry_run)


def copy_skills(
    src: Path,
    dst: Path,
    *,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    include_system: bool = False,
    prune: bool = False,
    dry_run: bool = False,
) -> tuple[syncing.SyncMessage, ...]:
    return syncing.copy_skills(
        src,
        dst,
        include=include,
        exclude=exclude,
        include_system=include_system,
        prune=prune,
        dry_run=dry_run,
    )


def source_skill_names(
    src: Path, *, include_system: bool = False
) -> tuple[str, ...]:
    return syncing.source_skill_names(src, include_system=include_system)


def selected_skill_names(
    src: Path,
    *,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    include_system: bool,
) -> tuple[str, ...]:
    return syncing.selected_skill_names(
        src,
        include=include,
        exclude=exclude,
        include_system=include_system,
    )


def read_skills_file(path: Path) -> tuple[str, ...]:
    return syncing.read_skills_file(path)


def validate_skill_name(value: str, option: str) -> str:
    return syncing.validate_skill_name(value, option)


class SyncService:
    """Run saved/explicit sync plans with injected presentation callbacks."""

    def __init__(
        self,
        mgr: Any,
        *,
        emit: SyncEmitter | None = None,
        confirm: SyncConfirm | None = None,
        render_hook: SyncHookRenderer | None = None,
        migrate_sessions: SyncSessionMigration | None = None,
        copy_skills_callback: SyncSkillCopy | None = None,
    ) -> None:
        self.mgr = mgr
        self.emit = emit or (lambda _level, _text: None)
        self.confirm = confirm
        self.render_hook = render_hook or (lambda _result: None)
        self.migrate_sessions = migrate_sessions or (lambda _mgr, _path: None)
        self.copy_skills_callback = copy_skills_callback

    def sync_profile(
        self,
        profile: str,
        *,
        yes: bool = False,
        sync_types: tuple[str, ...] | None = None,
        skill_include: tuple[str, ...] = (),
        skill_exclude: tuple[str, ...] = (),
        include_system_skills: bool = False,
        prune_skills: bool = False,
        dry_run: bool = False,
        extra_sync_types: tuple[str, ...] = (),
        migrations: Mapping[str, SyncMigration] | None = None,
    ) -> None:
        """Run explicit or saved migrations for one profile in order."""
        profile_path = self.mgr.profile_home(profile, must_exist=True)
        saved_mode = sync_types is None
        selected_types = (
            self.mgr.profile_sync_types(profile) if saved_mode else sync_types
        )
        if saved_mode and extra_sync_types:
            selected_types = tuple(
                dict.fromkeys((*selected_types, *extra_sync_types))
            )
        if not selected_types:
            self.emit("info", f"No saved sync types for profile '{profile}'.")
            return

        available = dict(migrations or self.migrations())
        for sync_type in selected_types:
            migration = available.get(sync_type)
            if migration is None:
                self.emit("warn", f"Unknown sync type '{sync_type}', skipped.")
                continue
            if saved_mode and sync_type == "plugins":
                # Before granular resource types existed, ``plugins`` meant
                # the all-in-one bundle. Preserve that meaning for saved
                # profiles; explicit ``--type plugins`` is plugin-only.
                migration = available["bundle"]
            if sync_type in syncing.SYNC_CONFIRM_TYPES and not yes and not dry_run:
                if self.confirm is None:
                    raise RuntimeError(
                        f"syncing {sync_type} requires an interactive confirmation"
                    )
                question = f"Sync {sync_type} into profile '{profile}'? "
                if sync_type == "skills" and prune_skills:
                    question += "Selected skills may be removed."
                else:
                    question += "Existing profile files may be overwritten."
                if not self.confirm(question):
                    self.emit("warn", f"Skipped {sync_type} for profile '{profile}'.")
                    continue
            self.emit("info", f"Syncing {sync_type} for profile '{profile}' ...")
            if sync_type == "skills":
                saved_options = self.mgr.profile_skill_sync_options(profile)
                include = skill_include
                exclude = skill_exclude
                include_system = include_system_skills
                prune = prune_skills
                if saved_options is not None:
                    # A bare --prune-skills applies the saved allowlist while
                    # opting into cleanup.
                    if (
                        not skill_include
                        and not skill_exclude
                        and not include_system_skills
                    ):
                        include = tuple(saved_options.get("include", ()))
                        exclude = tuple(saved_options.get("exclude", ()))
                        include_system = bool(
                            saved_options.get("include_system", False)
                        )
                    prune = prune_skills or bool(saved_options.get("prune", False))
                self._copy_skills(
                    self.mgr.config.source_home,
                    profile_path,
                    include=include,
                    exclude=exclude,
                    include_system=include_system,
                    prune=prune,
                    dry_run=dry_run,
                )
            elif dry_run:
                migration(self.mgr, profile_path, dry_run=True)
            else:
                # Preserve compatibility with third-party migrations that
                # predate the dry-run keyword.
                migration(self.mgr, profile_path)

    def persist_sync_configuration(
        self,
        profile: str,
        *,
        sync_types: tuple[str, ...],
        includes: tuple[str, ...],
        excludes: tuple[str, ...],
        include_system_skills: bool,
        prune_skills: bool,
        persist: bool,
        instructions: bool,
    ) -> None:
        """Persist a successfully applied sync plan."""
        if instructions:
            self.mgr.record_profile_sync_type(profile, "instructions")
        if not persist:
            return

        for sync_type in sync_types:
            if (
                sync_type in syncing.GRANULAR_SYNC_TYPES
                and sync_type != "skills"
            ):
                for legacy_type in LEGACY_BUNDLE_TYPES:
                    self.mgr.remove_profile_sync_type(profile, legacy_type)
            if sync_type == "skills":
                self.mgr.record_profile_skill_sync_options(
                    profile,
                    include=includes,
                    exclude=excludes,
                    include_system=include_system_skills,
                    prune=prune_skills,
                )
                continue

            # ``plugins`` is reserved for old profiles where it means the
            # full bundle; a newly persisted explicit plugin sync is plugin-only.
            saved_type = "plugins-only" if sync_type == "plugins" else sync_type
            self.mgr.record_profile_sync_type(profile, saved_type)

    def migrations(self) -> dict[str, SyncMigration]:
        """Return handlers for all public and compatibility sync types."""
        return {
            "skills": self._sync_skills,
            "plugins": self._sync_plugins,
            "bundle": self._sync_legacy_bundle,
            "plugins-only": self._sync_plugin_only_dirs,
            "agents": self._sync_agents,
            "mcp": self._sync_mcp,
            "rules": self._sync_rules,
            "prompts": self._sync_prompts,
            "instructions": self._sync_instructions,
            "config": self._sync_config,
            "statusline": self._sync_statusline,
            "hooks": self._sync_hooks,
            "sessions_shared": self._sync_shared_sessions,
            "sessions_migrate": self._sync_migrated_sessions,
        }

    def _emit_messages(self, messages: tuple[syncing.SyncMessage, ...]) -> None:
        for message in messages:
            self.emit(message.level, message.text)

    def _copy_resource_dirs(
        self,
        src: Path,
        dst: Path,
        names: tuple[str, ...],
        *,
        dry_run: bool = False,
        label: str = "resource",
    ) -> None:
        self._emit_messages(
            copy_resource_dirs(src, dst, names, dry_run=dry_run, label=label)
        )

    def _copy_plugin_dirs(self, src: Path, dst: Path, *, dry_run: bool = False) -> None:
        self._emit_messages(copy_plugin_dirs(src, dst, dry_run=dry_run))

    def _copy_plugin_only_dirs(
        self, src: Path, dst: Path, *, dry_run: bool = False
    ) -> None:
        self._emit_messages(copy_plugin_only_dirs(src, dst, dry_run=dry_run))

    def _copy_agents_dirs(self, src: Path, dst: Path, *, dry_run: bool = False) -> None:
        self._emit_messages(copy_agents_dirs(src, dst, dry_run=dry_run))

    def _copy_mcp_dirs(self, src: Path, dst: Path, *, dry_run: bool = False) -> None:
        self._emit_messages(copy_mcp_dirs(src, dst, dry_run=dry_run))

    def _copy_instruction_files(
        self, src: Path, dst: Path, *, dry_run: bool = False
    ) -> None:
        self._emit_messages(copy_instruction_files(src, dst, dry_run=dry_run))

    def _copy_core_config(self, src: Path, dst: Path, *, dry_run: bool = False) -> None:
        self._emit_messages(copy_core_config(src, dst, dry_run=dry_run))

    def _copy_skills(self, src: Path, dst: Path, **kwargs: Any) -> None:
        if self.copy_skills_callback is not None:
            self.copy_skills_callback(src, dst, **kwargs)
            return
        self._emit_messages(copy_skills(src, dst, **kwargs))

    def _sync_plugins(self, _mgr: Any, profile_path: Path, *, dry_run: bool = False) -> None:
        self._copy_plugin_only_dirs(
            self.mgr.config.source_home, profile_path, dry_run=dry_run
        )

    def _sync_legacy_bundle(
        self, _mgr: Any, profile_path: Path, *, dry_run: bool = False
    ) -> None:
        self._copy_plugin_dirs(
            self.mgr.config.source_home, profile_path, dry_run=dry_run
        )

    def _sync_plugin_only_dirs(
        self, _mgr: Any, profile_path: Path, *, dry_run: bool = False
    ) -> None:
        self._copy_plugin_only_dirs(
            self.mgr.config.source_home, profile_path, dry_run=dry_run
        )

    def _sync_agents(self, _mgr: Any, profile_path: Path, *, dry_run: bool = False) -> None:
        self._copy_agents_dirs(
            self.mgr.config.source_home, profile_path, dry_run=dry_run
        )

    def _sync_mcp(self, _mgr: Any, profile_path: Path, *, dry_run: bool = False) -> None:
        self._copy_mcp_dirs(
            self.mgr.config.source_home, profile_path, dry_run=dry_run
        )

    def _sync_rules(self, _mgr: Any, profile_path: Path, *, dry_run: bool = False) -> None:
        self._copy_resource_dirs(
            self.mgr.config.source_home,
            profile_path,
            ("rules",),
            dry_run=dry_run,
            label="rules",
        )

    def _sync_prompts(
        self, _mgr: Any, profile_path: Path, *, dry_run: bool = False
    ) -> None:
        self._copy_resource_dirs(
            self.mgr.config.source_home,
            profile_path,
            ("prompts",),
            dry_run=dry_run,
            label="prompts",
        )

    def _sync_instructions(
        self, _mgr: Any, profile_path: Path, *, dry_run: bool = False
    ) -> None:
        self._copy_instruction_files(
            self.mgr.config.source_home, profile_path, dry_run=dry_run
        )

    def _sync_config(self, _mgr: Any, profile_path: Path, *, dry_run: bool = False) -> None:
        self._copy_core_config(
            self.mgr.config.source_home, profile_path, dry_run=dry_run
        )

    def _sync_statusline(self, _mgr: Any, profile_path: Path, *, dry_run: bool = False) -> None:
        import tomllib

        src_config = self.mgr.config.source_home / "config.toml"
        dst_config = profile_path / "config.toml"

        if not src_config.is_file():
            self.emit("info", f"No source config.toml found at {src_config}.")
            return

        with open(src_config, "rb") as f:
            src_toml = tomllib.load(f)

        tui_config = src_toml.get("tui", {})
        status_line = tui_config.get("status_line")
        session_picker_view = tui_config.get("session_picker_view")

        if not status_line and not session_picker_view:
            self.emit("info", "No statusline configuration found in source config.toml.")
            return

        if dry_run:
            self.emit("info", "Would sync statusline configuration from root config.toml.")
            return

        # Read existing profile config
        dst_toml = {}
        if dst_config.is_file():
            with open(dst_config, "rb") as f:
                dst_toml = tomllib.load(f)

        # Update tui section
        if "tui" not in dst_toml:
            dst_toml["tui"] = {}

        if status_line:
            dst_toml["tui"]["status_line"] = status_line
        if session_picker_view:
            dst_toml["tui"]["session_picker_view"] = session_picker_view
        dst_toml["tui"]["status_line_use_colors"] = tui_config.get("status_line_use_colors", True)

        # Write back config.toml
        import tomli_w

        with open(dst_config, "wb") as f:
            tomli_w.dump(dst_toml, f)

        self.emit("success", "Synced statusline configuration from root config.toml.")

    def _sync_skills(self, _mgr: Any, profile_path: Path, *, dry_run: bool = False) -> None:
        self._copy_skills(
            self.mgr.config.source_home, profile_path, dry_run=dry_run
        )

    def _sync_hooks(self, _mgr: Any, profile_path: Path) -> None:
        self.render_hook(self.mgr.sync_profile_hooks(profile_path.name))

    def _sync_shared_sessions(self, _mgr: Any, profile_path: Path) -> None:
        for action in self.mgr.link_shared(
            profile_path, self.mgr.config.source_home
        ):
            self.emit("success", action.message)

    def _sync_migrated_sessions(self, _mgr: Any, profile_path: Path) -> None:
        self.migrate_sessions(self.mgr, profile_path)
