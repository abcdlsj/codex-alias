"""codex_alias — run multiple Codex profiles with isolated homes.

The package splits cleanly into a reusable, UI-free library and a rich+click
CLI:

    from codex_alias import CodexAlias, Config

    mgr = CodexAlias(Config.from_env())
    mgr.add_profile("work")
    for profile in mgr.list_profiles():
        print(profile.name)

Everything under this namespace raises :class:`CodexAliasError` subclasses instead
of printing or exiting, so the same core drives the CLI, tests, and any other
tooling.
"""

from __future__ import annotations

from .config import Config
from .errors import (
    AmbiguousSessionError,
    CodexAliasError,
    HomeNotFoundError,
    HookConfigError,
    InvalidNameError,
    ProfileConflictError,
    ProfileNotFoundError,
    RelayConfigError,
    RelayUnavailableError,
    SessionConflictError,
    SessionLossyMappingError,
    SessionNotFoundError,
    SessionRepairError,
)
from .manager import REF_CURRENT, REF_SOURCE, CodexAlias, validate_name
from .models import (
    CopyStatus,
    DetectedSession,
    DoctorReport,
    HomeKind,
    HomeRef,
    HookOption,
    HookSyncResult,
    LinkAction,
    Profile,
    ProfileRemoveResult,
    ProfileRenameResult,
    SessionCopyResult,
    SessionCloneResult,
    SessionFile,
    SessionFixResult,
)
from .relay import (
    RelayConfig,
    RelayLaunch,
    RelayLease,
    RelayService,
    RelayState,
    RelayStatus,
)

__version__ = "0.3.8"

__all__ = [
    "__version__",
    "Config",
    "CodexAlias",
    "validate_name",
    "REF_CURRENT",
    "REF_SOURCE",
    # models
    "CopyStatus",
    "DetectedSession",
    "DoctorReport",
    "HomeKind",
    "HomeRef",
    "HookOption",
    "HookSyncResult",
    "LinkAction",
    "Profile",
    "ProfileRemoveResult",
    "ProfileRenameResult",
    "SessionCopyResult",
    "SessionCloneResult",
    "SessionFile",
    "SessionFixResult",
    # relay
    "RelayConfig",
    "RelayLaunch",
    "RelayLease",
    "RelayService",
    "RelayState",
    "RelayStatus",
    # errors
    "CodexAliasError",
    "InvalidNameError",
    "ProfileConflictError",
    "ProfileNotFoundError",
    "RelayConfigError",
    "RelayUnavailableError",
    "HomeNotFoundError",
    "HookConfigError",
    "SessionNotFoundError",
    "AmbiguousSessionError",
    "SessionConflictError",
    "SessionLossyMappingError",
    "SessionRepairError",
]
