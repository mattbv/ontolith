"""Abstract storage backend port.

The StorageBackend port defines the interface that all concrete storage
adapters (SQLite, DuckDB, graph engines, etc.) must implement.
"""

from typing import Protocol


class StorageBackend(Protocol):
    """Abstract port for storage adapters.

    Concrete backends implement this protocol to provide:
    - Transaction management
    - Entity and assertion persistence
    - Query execution
    - Vector search (for hybrid retrieval)

    The default implementation (M1) will be SQLite + sqlite-vec.
    Alternative backends can be plugged in via this interface.

    This is a stub for M0. Full interface will be defined in M1 based on
    SPEC §12.3 requirements.
    """

    # Full interface to be defined in M1
    # Will include: begin(), commit(), rollback(), put_entity(),
    # put_assertion(), assertions(), vector_search(), etc.


__all__ = ["StorageBackend"]
