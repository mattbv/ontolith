"""Storage layer and backend abstractions."""

from ontolith.store.base import DEFAULT_NAMESPACE, VECTOR_SCOPES, StorageBackend

__all__ = ["StorageBackend", "VECTOR_SCOPES", "DEFAULT_NAMESPACE"]
