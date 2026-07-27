"""codex_alias — run multiple Codex profiles with isolated homes.

The package splits cleanly into a reusable, UI-free library and a rich+click
CLI:

    from codex_alias import CodexAlias, Config

    mgr = CodexAlias(Config.from_env())
    mgr.add_profile("work")
    for profile in mgr.list_profiles():
        print(profile.name)

Everything under this namespace raises :class:`CodexmError` subclasses instead
of printing or exiting, so the same core drives the CLI, tests, and any other
tooling.
"""

from __future__ import annotations

from .config import Config
from .errors import (
    AmbiguousSessionError,
    CodexmError,
    HomeNotFoundError,
    InvalidNameError,
    ProfileNotFoundError,
    SessionConflictError,
    SessionNotFoundError,
)
from .manager import REF_CURRENT, REF_SOURCE, CodexAlias, validate_name
from .models import (
    CopyStatus,
    DoctorReport,
    HomeKind,
    HomeRef,
    LinkAction,
    Profile,
    SessionCopyResult,
    SessionFile,
)

__version__ = "0.2.0"

__all__ = [
    "__version__",
    "Config",
    "CodexAlias",
    "validate_name",
    "REF_CURRENT",
    "REF_SOURCE",
    # models
    "CopyStatus",
    "DoctorReport",
    "HomeKind",
    "HomeRef",
    "LinkAction",
    "Profile",
    "SessionCopyResult",
    "SessionFile",
    # errors
    "CodexmError",
    "InvalidNameError",
    "ProfileNotFoundError",
    "HomeNotFoundError",
    "SessionNotFoundError",
    "AmbiguousSessionError",
    "SessionConflictError",
]
