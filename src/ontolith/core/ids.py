"""ID generation abstraction for deterministic testing.

The IdProvider port ensures entity and assertion IDs can be controlled in tests,
making bitemporal behavior reproducible.
"""

from abc import ABC, abstractmethod

from ulid import ULID


class IdProvider(ABC):
    """Abstract ID provider for deterministic ID generation."""

    @abstractmethod
    def next(self) -> str:
        """Generate the next ID.

        Returns:
            A unique identifier as a string.
        """
        ...


class UlidProvider(IdProvider):
    """Production ID provider using ULID.

    ULIDs are:
    - Lexicographically sortable
    - Timestamp-based (first 48 bits)
    - URL-safe
    - 26 characters (vs UUID's 36)
    - Compatible with UUID tools
    """

    def next(self) -> str:
        """Generate a new ULID.

        Returns:
            A ULID string (26 characters).
        """
        return str(ULID())


class SequentialIdProvider(IdProvider):
    """Test ID provider that generates sequential IDs.

    Useful for:
    - Deterministic testing (same IDs on every run)
    - Readable test fixtures ("id-001", "id-002", etc.)
    - Predictable ordering in tests

    Args:
        prefix: Prefix for generated IDs.
        start: Starting number (default 1).

    Example:
        >>> provider = SequentialIdProvider(prefix="test", start=1)
        >>> provider.next()
        'test-001'
        >>> provider.next()
        'test-002'
    """

    def __init__(self, prefix: str = "id", start: int = 1) -> None:
        """Initialize with optional prefix and starting number.

        Args:
            prefix: String prefix for IDs.
            start: Starting counter value.
        """
        self._prefix = prefix
        self._counter = start

    def next(self) -> str:
        """Generate the next sequential ID.

        Returns:
            A sequential ID string like "prefix-001".
        """
        current = self._counter
        self._counter += 1
        return f"{self._prefix}-{current:03d}"

    def reset(self, start: int = 1) -> None:
        """Reset the counter.

        Args:
            start: Value to reset counter to.
        """
        self._counter = start


class FixedIdProvider(IdProvider):
    """Test ID provider that cycles through a fixed list of IDs.

    Useful for tests that need specific, predictable IDs.

    Args:
        ids: List of IDs to cycle through.

    Example:
        >>> provider = FixedIdProvider(["alice-id", "bob-id"])
        >>> provider.next()
        'alice-id'
        >>> provider.next()
        'bob-id'
        >>> provider.next()  # cycles back
        'alice-id'
    """

    def __init__(self, ids: list[str]) -> None:
        """Initialize with a fixed list of IDs.

        Args:
            ids: List of ID strings to cycle through.

        Raises:
            ValueError: If ids list is empty.
        """
        if not ids:
            raise ValueError("FixedIdProvider requires at least one ID")
        self._ids = ids
        self._index = 0

    def next(self) -> str:
        """Return the next ID from the list, cycling if necessary.

        Returns:
            Next ID from the configured list.
        """
        id_str = self._ids[self._index]
        self._index = (self._index + 1) % len(self._ids)
        return id_str

    def reset(self) -> None:
        """Reset to the first ID in the list."""
        self._index = 0


__all__ = [
    "IdProvider",
    "UlidProvider",
    "SequentialIdProvider",
    "FixedIdProvider",
]
