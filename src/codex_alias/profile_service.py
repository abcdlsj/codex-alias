"""Profile-home lifecycle operations.

``ProfileStore`` owns the filesystem contract for named profiles.  Launch
construction lives in :mod:`codex_alias.launcher`; session, hook, and sync
operations stay in their respective modules.
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

from .config import Config
from .errors import CodexAliasError, ProfileConflictError, ProfileNotFoundError
from .launcher import ProfileLauncher
from .models import Profile, ProfileRemoveResult, ProfileRenameResult
from .validation import validate_name


class ProfileStore:
    """Create, enumerate, and remove profiles under one configured root."""

    def __init__(self, config: Config, launcher: ProfileLauncher | None = None) -> None:
        self.config = config
        self.launcher = launcher or ProfileLauncher(config)

    def list_profiles(self) -> list[Profile]:
        """Return profiles discovered directly under the configured root."""
        root = self.config.profile_root
        if not root.is_dir():
            return []
        return [
            Profile(
                name=path.name,
                path=path,
                sessions_shared=(path / "sessions").is_symlink(),
            )
            for path in sorted(path for path in root.iterdir() if path.is_dir())
        ]

    def profile_home(self, profile: str, *, must_exist: bool = False) -> Path:
        """Resolve a named profile home without creating it."""
        validate_name(profile, "profile")
        path = self.config.profile_path(profile)
        if must_exist and not path.is_dir():
            raise ProfileNotFoundError(f"profile not found: {path}")
        return path

    def add_profile(self, profile: str, command_name: str | None = None) -> Path:
        """Create a profile home and its wrapper command."""
        validate_name(profile, "profile")
        command_name = command_name or f"codex-{profile}"
        validate_name(command_name, "command name")

        profile_path = self.config.profile_path(profile)
        profile_path.mkdir(parents=True, exist_ok=True)
        self.config.bin_dir.mkdir(parents=True, exist_ok=True)

        target = self.config.wrapper_path(command_name)
        target.write_text(self.launcher.wrapper_script(profile), encoding="utf-8")
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return target

    def rename_profile(
        self,
        profile: str,
        new_profile: str,
        command_name: str | None = None,
        new_command_name: str | None = None,
    ) -> ProfileRenameResult:
        """Rename a profile home and update its generated wrapper safely."""
        validate_name(profile, "profile")
        validate_name(new_profile, "new profile")
        if profile == new_profile:
            raise ProfileConflictError(
                f"profile already has the requested name: {profile}"
            )

        old_path = self.config.profile_path(profile)
        new_path = self.config.profile_path(new_profile)
        if not old_path.is_dir():
            raise ProfileNotFoundError(f"profile not found: {old_path}")
        if self._path_exists(new_path):
            raise ProfileConflictError(f"profile already exists: {new_path}")

        old_command = command_name or f"codex-{profile}"
        new_command = new_command_name or f"codex-{new_profile}"
        validate_name(old_command, "command name")
        validate_name(new_command, "new command name")
        old_wrapper = self.config.wrapper_path(old_command)
        new_wrapper = self.config.wrapper_path(new_command)
        if old_wrapper != new_wrapper and self._path_exists(new_wrapper):
            raise ProfileConflictError(f"wrapper already exists: {new_wrapper}")

        wrapper_exists = self._path_exists(old_wrapper)
        if wrapper_exists and not old_wrapper.is_file():
            raise CodexAliasError(f"wrapper is not a regular file: {old_wrapper}")

        original_wrapper: bytes | None = None
        original_mode: int | None = None
        if wrapper_exists:
            try:
                original_wrapper = old_wrapper.read_bytes()
                original_mode = old_wrapper.stat().st_mode
            except OSError as exc:
                raise CodexAliasError(
                    f"could not read wrapper before renaming profile: {old_wrapper}"
                ) from exc

        home_moved = False
        new_wrapper_written = False
        old_wrapper_removed = False
        try:
            old_path.rename(new_path)
            home_moved = True

            if wrapper_exists:
                script = self.launcher.wrapper_script(new_profile)
                new_wrapper.write_text(script, encoding="utf-8")
                new_wrapper_written = True
                new_wrapper.chmod(original_mode or new_wrapper.stat().st_mode)
                if old_wrapper != new_wrapper:
                    old_wrapper.unlink()
                    old_wrapper_removed = True
        except OSError as exc:
            if old_wrapper_removed and original_wrapper is not None:
                try:
                    old_wrapper.write_bytes(original_wrapper)
                    if original_mode is not None:
                        old_wrapper.chmod(original_mode)
                except OSError:
                    pass
            if new_wrapper_written and old_wrapper != new_wrapper:
                try:
                    new_wrapper.unlink()
                except OSError:
                    pass
            if home_moved:
                try:
                    new_path.rename(old_path)
                except OSError:
                    pass
            if old_wrapper == new_wrapper and original_wrapper is not None:
                try:
                    old_wrapper.write_bytes(original_wrapper)
                    if original_mode is not None:
                        old_wrapper.chmod(original_mode)
                except OSError:
                    pass
            raise CodexAliasError(
                f"could not rename profile {profile!r} to {new_profile!r}: {exc}"
            ) from exc

        return ProfileRenameResult(
            old_profile=profile,
            profile=new_profile,
            old_profile_path=old_path,
            profile_path=new_path,
            old_wrapper_path=old_wrapper,
            wrapper_path=new_wrapper,
            wrapper_renamed=wrapper_exists,
        )

    def remove_wrapper(
        self, profile: str, command_name: str | None = None
    ) -> tuple[Path, bool]:
        """Delete a generated wrapper while leaving profile data intact."""
        validate_name(profile, "profile")
        command_name = command_name or f"codex-{profile}"
        validate_name(command_name, "command name")
        target = self.config.wrapper_path(command_name)
        if target.exists():
            target.unlink()
            return target, True
        return target, False

    def remove_profile(
        self,
        profile: str,
        command_name: str | None = None,
        *,
        keep_data: bool = False,
        source_home: Path,
        current_home: Path,
    ) -> ProfileRemoveResult:
        """Remove a profile wrapper and, unless requested, its home."""
        validate_name(profile, "profile")
        command_name = command_name or f"codex-{profile}"
        validate_name(command_name, "command name")

        profile_path = self.config.profile_path(profile)
        if not keep_data:
            if not profile_path.is_dir():
                raise ProfileNotFoundError(f"profile not found: {profile_path}")
            resolved = self._safe_resolve(profile_path)
            if resolved == self._safe_resolve(source_home):
                raise CodexAliasError(
                    f"refusing to remove {profile_path}: it is the configured source home"
                )
            if resolved == self._safe_resolve(current_home):
                raise CodexAliasError(
                    f"refusing to remove {profile_path}: it is the current CODEX_HOME"
                )

        wrapper_path = self.config.wrapper_path(command_name)
        wrapper_removed = False
        if wrapper_path.exists():
            wrapper_path.unlink()
            wrapper_removed = True

        home_removed = False
        if not keep_data:
            home_removed = self._remove_home(profile_path)

        return ProfileRemoveResult(
            profile=profile,
            profile_path=profile_path,
            wrapper_path=wrapper_path,
            wrapper_removed=wrapper_removed,
            home_removed=home_removed,
        )

    def refresh_wrappers(self) -> list[Path]:
        """Regenerate default wrapper commands for existing profiles."""
        return [self.add_profile(profile.name) for profile in self.list_profiles()]

    def _remove_home(self, profile_path: Path) -> bool:
        """Delete a profile home after verifying it stays under the root."""
        root = self._safe_resolve(self.config.profile_root)
        if root not in self._safe_resolve(profile_path).parents:
            raise CodexAliasError(
                f"refusing to remove path outside profile root: {profile_path}"
            )
        if profile_path.is_symlink():
            profile_path.unlink()
            return True
        if not profile_path.is_dir():
            return False
        shutil.rmtree(profile_path)
        return True

    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path.absolute()

    @staticmethod
    def _path_exists(path: Path) -> bool:
        """Treat broken symlinks as occupied paths during lifecycle changes."""
        return path.exists() or path.is_symlink()
