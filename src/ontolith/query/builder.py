"""Query builder for fluent entity retrieval.

Per SPEC §12.2: Query builder provides .where() filtering and traversal.
For M1 we implement basic filtering; full traversal is M2.
"""

from typing import Any

from ontolith.core import Entity
from ontolith.store.base import StorageBackend


class QueryBuilder:
    """Fluent query interface for entities.

    Example:
        >>> kb.query(Person).where(name="Ada Lovelace")
        >>> kb.query(Person).where(employer__name="Acme Corp")  # future: traversal
    """

    def __init__(
        self,
        backend: StorageBackend,
        namespace: str,
        concept: str,
    ) -> None:
        """Initialize query builder.

        Args:
            backend: Storage backend for retrieval
            namespace: Namespace to query in
            concept: Concept to filter by
        """
        self._backend = backend
        self._namespace = namespace
        self._concept = concept
        self._filters: dict[str, Any] = {}

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
        # M1: Get all entities of this concept, then filter client-side
        # M2: Push filters to backend for efficiency
        entities = self._backend.entities(
            namespace=self._namespace,
            concept=self._concept,
        )

        if not self._filters:
            return entities

        # Client-side filtering (M1 simplification)
        filtered = []
        for entity in entities:
            # Get assertions for this entity
            assertions = self._backend.assertions(subject=entity.id, status="active")

            # Build property map
            props = {}
            for assertion in assertions:
                # Extract property name from predicate (e.g., "Person.name" -> "name")
                if "." in assertion.predicate:
                    prop_name = assertion.predicate.split(".", 1)[1]
                    props[prop_name] = assertion.value

            # Check if all filters match
            if all(props.get(k) == v for k, v in self._filters.items()):
                filtered.append(entity)

        return filtered

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
