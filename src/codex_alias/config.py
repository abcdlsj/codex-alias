"""Configuration for codexm, resolved from environment with sane defaults.

Mirrors the environment contract of the original shell tool:

- CODEXSWITCH_PROFILE_ROOT  -> profile_root   (default ~/.codex/profiles)
- CODEXSWITCH_BIN_DIR       -> bin_dir        (default ~/.local/bin)
- CODEXSWITCH_CODEX_CMD     -> codex_cmd      (default "codex")
- CODEXSWITCH_CODEX_WRAPPER -> codex_wrapper  (optional executable wrapper)
- CODEXSWITCH_SOURCE_HOME   -> source_home    (default $CODEX_HOME or ~/.codex)
- CODEXSWITCH_MANAGER_BIN_NAME -> manager_bin (default "codexm")
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _expand(path: str) -> Path:
    return Path(path).expanduser()


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved runtime configuration.

    Instances are immutable; build one with :meth:`from_env` (the common case)
    or construct directly in tests to pin every path.
    """

    profile_root: Path
    bin_dir: Path
    codex_cmd: str
    source_home: Path
    manager_bin_name: str
    codex_wrapper: str | None = None

    @property
    def effective_codex_cmd(self) -> str:
        """Executable used to launch Codex, preferring an explicit wrapper."""
        return self.codex_wrapper or self.codex_cmd

    @classmethod
    def from_env(cls, environ: os._Environ | dict[str, str] | None = None) -> "Config":
        env = os.environ if environ is None else environ
        home = Path(env.get("HOME", str(Path.home())))
        default_source = env.get("CODEX_HOME") or str(home / ".codex")

        return cls(
            profile_root=_expand(
                env.get("CODEXSWITCH_PROFILE_ROOT", str(home / ".codex" / "profiles"))
            ),
            bin_dir=_expand(
                env.get("CODEXSWITCH_BIN_DIR", str(home / ".local" / "bin"))
            ),
            codex_cmd=env.get("CODEXSWITCH_CODEX_CMD", "codex"),
            source_home=_expand(
                env.get("CODEXSWITCH_SOURCE_HOME", default_source)
            ),
            manager_bin_name=env.get("CODEXSWITCH_MANAGER_BIN_NAME", "codexalias"),
            codex_wrapper=env.get("CODEXSWITCH_CODEX_WRAPPER") or None,
        )

    def profile_path(self, profile: str) -> Path:
        """Absolute home directory for a named profile."""
        return self.profile_root / profile

    def wrapper_path(self, command_name: str) -> Path:
        """Absolute path of a generated wrapper command."""
        return self.bin_dir / command_name
