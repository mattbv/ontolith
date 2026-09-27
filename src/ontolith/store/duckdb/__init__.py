"""DuckDB storage backend.

Second pluggable storage adapter (M3, ADR-0016), proving the StorageBackend
port abstraction against a structurally different embedded engine than
SQLite.
"""

from ontolith.store.duckdb import migrations
from ontolith.store.duckdb.backend import DuckDBBackend

__all__ = ["DuckDBBackend", "migrations"]
