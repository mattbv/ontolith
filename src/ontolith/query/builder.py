"""Query builder for fluent entity retrieval.

Per SPEC §12.2: Query builder provides .where() filtering and traversal.
SPEC §11.3 additionally names semantic (vector) search — see .semantic().
"""

from datetime import datetime
from typing import Any

from ontolith.core import Embedder, Entity
from ontolith.core.errors import ValidationError
from ontolith.store.base import StorageBackend

# Ranking algorithm (ADR-0020 amendment): vector-search-first, symbolic-intersect.
# .semantic() overfetches this many candidates by default (or 10x the caller's
# .limit(), whichever is larger) before intersecting with .where() and applying
# confidence/trust filters — bounded cost beats exactness for a p95 budget, at
# the cost of a true symbolic match ranked below the overfetch window in the
# global vector ranking being missed.
_DEFAULT_OVERFETCH = 20
_OVERFETCH_MULTIPLIER = 10
_MAX_OVERFETCH = 1000


class QueryBuilder:
    """Fluent query interface for entities.

    Example:
        >>> kb.query(Person).where(name="Ada Lovelace")
        >>> kb.as_of("2025-01-01").query(Person).where(employer__name="Acme Corp")
        >>> kb.query(Person).semantic("a computer scientist").limit(5).all()
    """

    def __init__(
        self,
        backend: StorageBackend,
        namespace: str,
        concept: str,
        as_of_time: datetime | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        """Initialize query builder.

        Args:
            backend: Storage backend for retrieval
            namespace: Namespace to query in
            concept: Concept to filter by
            as_of_time: If set, applies bitemporal filter to all queries
            embedder: Embedder used to embed .semantic() query text. Required
                only if .semantic() is called.
        """
        self._backend = backend
        self._namespace = namespace
        self._concept = concept
        self._filters: dict[str, Any] = {}
        self._as_of_time = as_of_time
        self._embedder = embedder
        self._semantic_text: str | None = None
        self._min_confidence: float | None = None
        self._trust_at_least: int | None = None
        self._limit: int | None = None

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

    def semantic(self, text: str) -> "QueryBuilder":
        """Rank results by vector similarity to `text` (SPEC §11.3).

        Results are embedded entities (see `Ontology.reindex()`) ranked by
        ascending distance to `text`'s embedding. Combine with `.where()` to
        intersect with symbolic filters, preserving vector rank order.

        Args:
            text: Query text, embedded via this builder's Embedder.

        Returns:
            Self for chaining
        """
        self._semantic_text = text
        return self

    def min_confidence(self, threshold: float) -> "QueryBuilder":
        """Keep only entities with at least one active assertion at or above
        `threshold` confidence.

        An entity with only `confidence=None` assertions does not pass —
        None never satisfies a numeric threshold (ADR-0004). Independent of
        `.trust_at_least()`: the qualifying assertion need not be the same
        one for both filters. Ignores `.as_of()` — always checks
        currently-active assertions regardless of any bitemporal view
        pinned on this query (KI-036).

        Args:
            threshold: Minimum confidence, 0.0-1.0.

        Returns:
            Self for chaining
        """
        self._min_confidence = threshold
        return self

    def trust_at_least(self, level: int) -> "QueryBuilder":
        """Keep only entities with at least one active assertion authored by
        a principal whose trust_level >= `level`.

        Ignores `.as_of()` — always checks currently-active assertions and
        current principal trust levels regardless of any bitemporal view
        pinned on this query (KI-036).

        Args:
            level: Minimum principal trust level, 0-10.

        Returns:
            Self for chaining
        """
        self._trust_at_least = level
        return self

    def limit(self, n: int) -> "QueryBuilder":
        """Cap the number of entities `.all()` returns.

        Args:
            n: Maximum number of results.

        Returns:
            Self for chaining
        """
        self._limit = n
        return self

    def all(self) -> list[Entity]:
        """Execute query and return all matching entities.

        Returns:
            List of matching entities (may be empty)
        """
        entities = self._base_candidates()
        entities = self._apply_confidence_trust_filters(entities)
        if self._limit is not None:
            entities = entities[: self._limit]
        return entities

    def _base_candidates(self) -> list[Entity]:
        """Resolve the pre-confidence/trust/limit candidate list."""
        if self._semantic_text is not None:
            return self._semantic_candidates()
        if not self._filters:
            return self._backend.entities(
                namespace=self._namespace,
                concept=self._concept,
                as_of_time=self._as_of_time,
            )
        return self._backend.entities_where(
            namespace=self._namespace,
            concept=self._concept,
            predicate_filters=self._qualified_filters(),
            as_of_time=self._as_of_time,
        )

    def _qualified_filters(self) -> dict[str, str]:
        """Push .where() filters to backend as full predicates: "name" -> "Concept.name"."""
        return {f"{self._concept}.{key}": value for key, value in self._filters.items()}

    def _semantic_candidates(self) -> list[Entity]:
        """Vector-search-first ranking, optionally intersected with .where().

        Overfetches from the vector index, then (if .where() is also set)
        intersects with the symbolic matches while preserving vector rank
        order — a true symbolic match ranked below the overfetch window in
        the global vector ranking is missed (see the module-level comment).

        Raises:
            ValidationError: No Embedder was configured on this QueryBuilder.
        """
        if self._embedder is None:
            raise ValidationError(
                "semantic() requires an Embedder; none was configured on this QueryBuilder"
            )
        assert self._semantic_text is not None

        query_vec = self._embedder.embed([self._semantic_text])[0]
        limit_val = self._limit if self._limit is not None else _DEFAULT_OVERFETCH
        overfetch = min(max(limit_val, _OVERFETCH_MULTIPLIER * limit_val), _MAX_OVERFETCH)
        ranked = self._backend.vector_search("entity", query_vec, overfetch)
        ranked_ids = [entity_id for entity_id, _ in ranked]

        if self._filters:
            symbolic_ids = {
                e.id
                for e in self._backend.entities_where(
                    namespace=self._namespace,
                    concept=self._concept,
                    predicate_filters=self._qualified_filters(),
                    as_of_time=self._as_of_time,
                )
            }
            ranked_ids = [entity_id for entity_id in ranked_ids if entity_id in symbolic_ids]

        entities = []
        for entity_id in ranked_ids:
            entity = self._backend.get_entity(entity_id)
            if (
                entity is not None
                and entity.namespace == self._namespace
                and entity.concept == self._concept
            ):
                entities.append(entity)
        return entities

    def _apply_confidence_trust_filters(self, entities: list[Entity]) -> list[Entity]:
        """Apply .min_confidence()/.trust_at_least() as independent existential filters.

        Pushed down to the backend as a bulk `(namespace, concept)`-scoped
        lookup (KI-028) rather than one assertions()/get_principal() round
        trip per candidate entity — one SQL round trip per active filter,
        regardless of how many candidates `entities` holds or how large the
        concept is (an id-list-bound query wouldn't have that second
        property: a `WHERE id IN (...)` with one placeholder per candidate
        hits SQLite's bound-variable limit, and costs DuckDB per-parameter
        bind overhead, at real-world scale).
        """
        if self._min_confidence is None and self._trust_at_least is None:
            return entities
        if not entities:
            return entities

        qualifying_ids: set[str] | None = None

        if self._min_confidence is not None:
            qualifying_ids = self._backend.entities_meeting_confidence(
                self._namespace, self._concept, self._min_confidence
            )

        if self._trust_at_least is not None:
            trust_ids = self._backend.entities_meeting_trust(
                self._namespace, self._concept, self._trust_at_least
            )
            qualifying_ids = trust_ids if qualifying_ids is None else qualifying_ids & trust_ids

        assert qualifying_ids is not None
        return [e for e in entities if e.id in qualifying_ids]

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
