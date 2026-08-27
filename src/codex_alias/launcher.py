"""Codex process construction for isolated profile homes.

This module deliberately does not execute a process.  It only builds the
argv/environment pair so the CLI and library callers can decide whether to
``exec`` or inspect the launch.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import Config
from .validation import validate_name


class ProfileLauncher:
    """Build Codex launches with one profile's ``CODEX_HOME``."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def run_argv(
        self, profile: str, args: list[str]
    ) -> tuple[list[str], dict[str, str]]:
        """Build a one-shot launch under ``profile`` and create its home."""
        validate_name(profile, "profile")
        profile_path = self.config.profile_path(profile)
        profile_path.mkdir(parents=True, exist_ok=True)
        return self._with_home(profile_path, args)

    def resume_argv(
        self, home: Path, session_id: str, args: list[str] | None = None
    ) -> tuple[list[str], dict[str, str]]:
        """Build a resume launch under an already-resolved home."""
        return self._with_home(home, ["resume", session_id, *(args or [])])

    def wrapper_script(self, profile: str) -> str:
        """Return the generated shell wrapper for ``profile``."""
        validate_name(profile, "profile")
        return (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'exec "${{CODEXALIAS_MANAGER_BIN_NAME:-codexalias}}" run {profile} "$@"\n'
        )

    def codex_argv(self, args: list[str]) -> list[str]:
        """Build a launch that follows the user's configured Codex command."""
        fixed_args = [*self.config.codex_args, *args]
        if self.config.codex_wrapper or self.config.codex_cmd != "codex":
            return [self.config.effective_codex_cmd, *fixed_args]

        shell = os.environ.get("SHELL")
        if not shell:
            return ["codex", *fixed_args]

        shell_name = Path(shell).name
        if shell_name == "fish":
            # ``--`` prevents leading Codex flags from being consumed as fish
            # interpreter options; arguments after it populate fish's $argv.
            return [shell, "-ic", "codex $argv", "--", *fixed_args]
        if shell_name in {"sh", "bash", "zsh", "dash", "ksh"}:
            # The first argument after the command string becomes $0; the
            # remaining arguments are exposed through "$@".
            return [shell, "-ic", 'codex "$@"', "codex", *fixed_args]

        # Unknown shell syntax is safer to bypass than to guess.
        return ["codex", *fixed_args]

    def _with_home(
        self, home: Path, args: list[str]
    ) -> tuple[list[str], dict[str, str]]:
        env = dict(os.environ)
        env["CODEX_HOME"] = str(home)
        return self.codex_argv(args), env
