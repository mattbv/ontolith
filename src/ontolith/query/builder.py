"""Query builder for fluent entity retrieval.

Per SPEC §12.2: Query builder provides .where() filtering and traversal.
For M1 we implement basic filtering; full traversal is M2.
"""

from datetime import datetime
from typing import Any

from ontolith.core import Entity
from ontolith.store.base import StorageBackend


class QueryBuilder:
    """Fluent query interface for entities.

    Example:
        >>> kb.query(Person).where(name="Ada Lovelace")
        >>> kb.as_of("2025-01-01").query(Person).where(employer__name="Acme Corp")
    """

    def __init__(
        self,
        backend: StorageBackend,
        namespace: str,
        concept: str,
        as_of_time: datetime | None = None,
    ) -> None:
        """Initialize query builder.

        Args:
            backend: Storage backend for retrieval
            namespace: Namespace to query in
            concept: Concept to filter by
            as_of_time: If set, applies bitemporal filter to all queries
        """
        self._backend = backend
        self._namespace = namespace
        self._concept = concept
        self._filters: dict[str, Any] = {}
        self._as_of_time = as_of_time

    def where(self, **kwargs: Any) -> "QueryBuilder":
        """Add filters to the query.

        For M1, filters are simple equality checks on properties.
        M2 will add relation traversal (employer__name syntax).

        Args:
            **kwargs: Property filters as keyword arguments

        Returns:
            Self for chaining
        """
        self._filters.update(kwargs)
        return self

    def all(self) -> list[Entity]:
        """Execute query and return all matching entities.

        Returns:
            List of matching entities (may be empty)
        """
        if not self._filters:
            return self._backend.entities(
                namespace=self._namespace,
                concept=self._concept,
                as_of_time=self._as_of_time,
            )

        # Push filters to backend as full predicates: "name" → "Concept.name"
        predicate_filters = {
            f"{self._concept}.{key}": value for key, value in self._filters.items()
        }
        return self._backend.entities_where(
            namespace=self._namespace,
            concept=self._concept,
            predicate_filters=predicate_filters,
            as_of_time=self._as_of_time,
        )

    def first(self) -> Entity | None:
        """Execute query and return first matching entity.

        Returns:
            First matching entity, or None if no matches
        """
        results = self.all()
        return results[0] if results else None

    def count(self) -> int:
        """Execute query and return count of matching entities.

        Returns:
            Number of matching entities
        """
        return len(self.all())


__all__ = ["QueryBuilder"]
