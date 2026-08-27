"""Value objects returned by the library.

These are plain data carriers with no behaviour and no I/O, so callers (CLI,
tests, other tools) can consume results without depending on rendering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class HomeKind(Enum):
    """How a Codex home relates to the current configuration."""

    CURRENT = "current"
    SOURCE = "source"
    PROFILE = "profile"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class HomeRef:
    """A resolved Codex home together with a human-friendly label."""

    path: Path
    kind: HomeKind
    profile: str | None = None

    @property
    def label(self) -> str:
        if self.kind is HomeKind.PROFILE and self.profile:
            return f"profile:{self.profile} ({self.path})"
        return f"{self.kind.value} ({self.path})"


@dataclass(frozen=True, slots=True)
class Profile:
    """A named profile discovered under the profile root."""

    name: str
    path: Path
    sessions_shared: bool


@dataclass(frozen=True, slots=True)
class ProfileRemoveResult:
    """Outcome of removing a profile wrapper and (optionally) its home."""

    profile: str
    profile_path: Path
    wrapper_path: Path
    wrapper_removed: bool
    home_removed: bool


@dataclass(frozen=True, slots=True)
class SessionFile:
    """A single Codex session record on disk."""

    session_id: str
    path: Path
    relative_path: str


@dataclass(frozen=True, slots=True)
class DetectedSession:
    """The most recent session found for a working directory."""

    session_id: str
    profile: str | None
    home: Path
    path: Path
    cwd: Path
    last_output: str | None = None
    updated_at: float | None = None

    @property
    def output(self) -> str | None:
        """Compatibility alias for callers that call the final text output."""
        return self.last_output

    @property
    def profile_name(self) -> str | None:
        """Compatibility alias for the inferred profile name."""
        return self.profile


class CopyStatus(Enum):
    COPIED = "copied"
    SKIPPED = "skipped"  # already present, identical content


@dataclass(frozen=True, slots=True)
class SessionCopyResult:
    session_id: str
    status: CopyStatus


@dataclass(frozen=True, slots=True)
class SessionFixResult:
    """Summary of provider/model repairs performed on one session file."""

    session_id: str
    provider: str
    previous_providers: tuple[str, ...]
    changed_records: int
    changed_fields: int
    backup_path: Path | None
    dry_run: bool
    state_changed: bool = False
    state_backup_path: Path | None = None
    model: str | None = None
    previous_models: tuple[str, ...] = ()
    changed_model_fields: int = 0
    mapped_records: int = 0
    applied_mappings: tuple[str, ...] = ()
    dropped_records: int = 0
    lossy_mappings: tuple[str, ...] = ()
    mapping_warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionCloneResult:
    """A provider-adapted copy created for resume under another profile."""

    source_session_id: str
    session_id: str
    provider: str
    path: Path
    target_home: Path
    model: str | None = None
    mapped_records: int = 0
    applied_mappings: tuple[str, ...] = ()
    dropped_records: int = 0
    lossy_mappings: tuple[str, ...] = ()
    mapping_warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LinkAction:
    """One filesystem link/backup performed while sharing sessions."""

    message: str


@dataclass(frozen=True, slots=True)
class HookOption:
    """One selectable leaf hook from the root profile."""

    key: str
    event: str
    matcher: str
    hook_type: str
    detail: str
    source: str = "root"
    selected: bool = False


@dataclass(frozen=True, slots=True)
class HookSyncResult:
    """Summary of syncing selected root hooks into one profile."""

    source_path: Path
    target_path: Path
    selected_count: int
    added: int
    removed: int
    missing: tuple[str, ...] = ()
    backup_path: Path | None = None
    changed: bool = False


@dataclass(slots=True)
class DoctorReport:
    """Environment snapshot and sanity checks."""

    codex_cmd: str
    codex_wrapper: str | None
    effective_codex_cmd: str
    codex_args: tuple[str, ...]
    source_home: Path
    profile_root: Path
    bin_dir: Path
    manager_bin_name: str
    bin_on_path: bool
    codex_path: str | None
    warnings: list[str] = field(default_factory=list)

    @property
    def codex_present(self) -> bool:
        return self.codex_path is not None
