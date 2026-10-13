"""Errors shared by local Git workspace implementations."""

class WorktreeError(RuntimeError):
    """Raised when a Worktree cannot be created or its binding is invalid."""
