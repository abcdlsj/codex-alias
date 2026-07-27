"""Typed errors for the codexalias library.

The library never prints or exits; it raises. The CLI layer decides how to
render these to the user.
"""

from __future__ import annotations


class CodexAliasError(Exception):
    """Base class for all recoverable codexalias errors."""


class InvalidNameError(CodexAliasError):
    """A profile or command name contains disallowed characters."""


class ProfileNotFoundError(CodexAliasError):
    """The requested profile does not exist on disk."""


class HomeNotFoundError(CodexAliasError):
    """A referenced Codex home / directory does not exist."""


class SessionNotFoundError(CodexAliasError):
    """No session matched the given query in the source home."""


class AmbiguousSessionError(CodexAliasError):
    """A session query matched more than one session file."""

    def __init__(self, query: str, matches: list[str]) -> None:
        self.query = query
        self.matches = matches
        preview = "\n".join(f"  - {m}" for m in matches[:10])
        super().__init__(
            f"multiple sessions matched {query!r}; be more specific:\n{preview}"
        )


class SessionConflictError(CodexAliasError):
    """A target session already exists with different content."""


class SessionRepairError(CodexAliasError):
    """A session cannot be inspected or repaired safely."""
