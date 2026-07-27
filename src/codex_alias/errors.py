"""Typed errors for the codexm library.

The library never prints or exits; it raises. The CLI layer decides how to
render these to the user.
"""

from __future__ import annotations


class CodexmError(Exception):
    """Base class for all recoverable codexm errors."""


class InvalidNameError(CodexmError):
    """A profile or command name contains disallowed characters."""


class ProfileNotFoundError(CodexmError):
    """The requested profile does not exist on disk."""


class HomeNotFoundError(CodexmError):
    """A referenced Codex home / directory does not exist."""


class SessionNotFoundError(CodexmError):
    """No session matched the given query in the source home."""


class AmbiguousSessionError(CodexmError):
    """A session query matched more than one session file."""

    def __init__(self, query: str, matches: list[str]) -> None:
        self.query = query
        self.matches = matches
        preview = "\n".join(f"  - {m}" for m in matches[:10])
        super().__init__(
            f"multiple sessions matched {query!r}; be more specific:\n{preview}"
        )


class SessionConflictError(CodexmError):
    """A target session already exists with different content."""


class SessionRepairError(CodexmError):
    """A session cannot be inspected or repaired safely."""
