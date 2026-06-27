"""SQLite storage backend.

Default storage adapter for Ontolith using SQLite3 with bitemporal schema.
"""

from ontolith.store.sqlite.backend import SQLiteBackend

__all__ = ["SQLiteBackend"]
