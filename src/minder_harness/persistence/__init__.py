"""SQLite infrastructure; the agent loop has no dependency on this package."""

from .sqlite import SQLiteExecutionStore

__all__ = ["SQLiteExecutionStore"]
