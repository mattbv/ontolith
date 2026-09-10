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

_LOOKUP_OPERATORS = frozenset({"contains", "gt", "lt", "gte", "lte"})
_RANGE_OPERATORS = frozenset({"gt", "lt", "gte", "lte"})
_RANGE_VALUE_TYPES = frozenset({"Integer", "Float"})
"""value_type_of() results .where()'s __gt/__lt/__gte/__lte accept (KI-039).
value_lit is always stored as TEXT (SPEC §12.2), so an ordering comparison
needs CAST(value_lit AS REAL) — correct for Integer/Float, silently wrong
for anything else (a Text-typed predicate would compare nonsense; a
Date-typed one happens to sort correctly as a string today, but not as a
number, which isn't worth the inconsistency of special-casing). Restricting
to this set means a predicate the schema doesn't declare, declares as
non-numeric, or resolves to a relation (SchemaIR.value_type_of() returns
None for a RelationDef) all fail loudly via ValidationError instead of
silently producing a wrong or nonsensical comparison."""

_CANDIDATE_HINT_MAX = 1000
"""Ceiling on how large a `.where()`/`.semantic()`-narrowed candidate set can
be before it's passed down as `entities_meeting_confidence`/
`entities_meeting_trust`'s optional `candidate_ids` hint (KI-037). Measured
on SQLite: narrowing is a clear win through several thousand candidates
against a 50k-entity concept, and a measured regression past ~15-25k (the
JSON-encoded `IN`-subquery itself becomes the bottleneck) — this constant
keeps real margin below that crossover. Matches `_MAX_OVERFETCH` so a
`.semantic()`-narrowed set is always within range; a `.where()`-narrowed set
on a low-selectivity predicate can still exceed it, in which case the hint
is simply not passed (safe — see `_apply_confidence_trust_filters`)."""


class QueryBuilder:
    """Fluent query interface for entities.

    Example:
        >>> kb.query(Person).where(name="Ada Lovelace")
        >>> kb.as_of("2025-01-01").query(Person).where(employer="org-123")
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
        self._filters: dict[tuple[str, str], Any] = {}
        self._as_of_time = as_of_time
        self._embedder = embedder
        self._semantic_text: str | None = None
        self._min_confidence: float | None = None
        self._trust_at_least: int | None = None
        self._limit: int | None = None
        self._include_flagged: bool = False
        self._include_history: bool = False

    def where(self, **kwargs: Any) -> "QueryBuilder":
        """Add filters to the query.

        Plain keys are equality checks against the concept's own predicates
        — both literal properties (`name="Ada Lovelace"`) and relations,
        where the value is compared against the relation's target entity id
        (`employer="org-123"`). A closed set of double-underscore ("dunder")
        lookup-operator suffixes is also recognized (KI-039):
        `__contains` (substring match, literal properties only) and
        `__gt`/`__lt`/`__gte`/`__lte` (numeric range, restricted to
        predicates the active schema declares `Integer` or `Float` — see
        `_RANGE_VALUE_TYPES`'s docstring for why). Calling `.where()` more
        than once with the same key AND the same operator overwrites the
        earlier value (last call wins), matching plain equality's existing
        behavior; different operators on the same property compose as AND
        (e.g. `.where(age__gte=18, age__lt=65)`). Multi-hop relation
        traversal (a hypothetical `employer__name=`, ADR-0027) and any
        dunder suffix outside this closed operator set are still rejected
        outright — they never matched anything before KI-030 fixed that.

        Args:
            **kwargs: Property/relation filters as keyword arguments,
                optionally suffixed with a recognized lookup operator

        Returns:
            Self for chaining

        Raises:
            ValidationError: A filter key uses relation-traversal or an
                unrecognized dunder suffix, or a `__gt`/`__lt`/`__gte`/
                `__lte` filter targets a non-numeric (or relation, or
                undeclared) predicate, or its value doesn't parse as a
                number.
        """
        for key, value in kwargs.items():
            prop, operator = self._parse_filter_key(key)
            if operator in _RANGE_OPERATORS:
                value = self._coerce_range_value(prop, operator, value)
            elif operator == "contains" and not isinstance(value, str):
                raise ValidationError(f"where({prop}__contains={value!r}) requires a string value")
            self._filters[(prop, operator)] = value
        return self

    def _parse_filter_key(self, key: str) -> tuple[str, str]:
        """Split a `.where()` kwarg key into `(property, operator)` (KI-039).

        No `__` means plain equality (`"eq"`). A recognized lookup-operator
        suffix (see `_LOOKUP_OPERATORS`) splits the key into the property
        and that operator. Anything else — an unrecognized suffix, more than
        one `__` in the key (multi-hop relation traversal), or an empty
        property name (a key that IS just the operator suffix, e.g.
        `__contains`) — raises.
        """
        if "__" not in key:
            return key, "eq"
        prop, _, suffix = key.rpartition("__")
        if suffix not in _LOOKUP_OPERATORS or "__" in prop or not prop:
            raise ValidationError(
                f"where({key}=...) is not supported: recognized lookup operators are "
                f"{sorted(_LOOKUP_OPERATORS)} (KI-039); relation traversal (e.g. "
                "employer__name, ADR-0027) is still not implemented. Filter on this "
                "concept's own properties or a relation's target id directly with "
                "equality (e.g. employer=<id>)."
            )
        return prop, suffix

    def _coerce_range_value(self, prop: str, operator: str, value: Any) -> float:
        """Validate and convert a `__gt`/`__lt`/`__gte`/`__lte` value (KI-039).

        Validates against the schema effective at `.as_of()`'s pinned time,
        not today's, when this query is bitemporally pinned (SPEC §11.4) —
        a predicate retyped since `t` must be judged by what it was
        declared at `t`, the same way `AsOfView.schema()` already resolves
        (KI-019).

        Raises:
            ValidationError: No schema was registered (as of the relevant
                time) for this namespace, the predicate isn't declared in
                it, it's declared with a non-`Integer`/`Float` value_type
                (including relations, which have none), or `value` doesn't
                parse as a number.
        """
        qualified = f"{self._concept}.{prop}"
        schema = (
            self._backend.get_schema_at(self._namespace, self._as_of_time)
            if self._as_of_time is not None
            else self._backend.get_schema(self._namespace)
        )
        value_type = schema.value_type_of(qualified) if schema is not None else None
        if value_type not in _RANGE_VALUE_TYPES:
            detail = (
                "no schema is registered for this namespace"
                if schema is None
                else f"got value_type={value_type!r}"
            )
            raise ValidationError(
                f"where({prop}__{operator}=...) requires {qualified!r} to be declared "
                f"Integer or Float in the active schema (KI-039) — {detail}."
            )
        # bool is a subclass of int, so float(True) == 1.0 would otherwise
        # silently accept a boolean as if it were a real numeric filter.
        if isinstance(value, bool):
            raise ValidationError(f"where({prop}__{operator}={value!r}) is not a number")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"where({prop}__{operator}={value!r}) is not a number") from exc

    def semantic(self, text: str) -> "QueryBuilder":
        """Rank results by vector similarity to `text` (SPEC §11.3).

        Results are embedded entities (see `Ontology.reindex()`) ranked by
        ascending distance to `text`'s embedding. Combine with `.where()` to
        intersect with symbolic filters, preserving vector rank order.

        Combined with `.as_of()`: an entity that didn't exist yet at that
        point in time is excluded (KI-058), but the ranking itself is not
        bitemporal — the vector index holds one embedding per entity, as of
        whenever `Ontology.reindex()` was last called, with no historical
        versions. Results are always ranked by an entity's *current*
        embedded content, never its content as it stood at `as_of_time`.

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
        one for both filters. Respects `.as_of()` (KI-036): if this query
        is pinned to a point in time, the qualifying assertion must be
        active at that time, not merely currently active.

        Args:
            threshold: Minimum confidence, 0.0-1.0.

        Returns:
            Self for chaining
        """
        self._min_confidence = threshold
        return self

    def trust_at_least(self, level: int) -> "QueryBuilder":
        """Keep only entities with at least one active assertion whose
        *effective* trust_level >= `level`.

        "Effective" (KI-047): for an assertion made under delegation
        (`acting_as` set), this is `min(author.trust_level,
        acting_as.trust_level)` — the same effective-trust formula
        `govern/policy.py` already uses to decide whether to auto-accept
        that same assertion, by analogy with SPEC §8.4's capability rule
        ("effective capability is min(author, acting_as)") — not the
        author's raw trust_level alone. For a non-delegated assertion it's
        simply the author's own trust_level.

        Respects `.as_of()` (KI-036) for which assertion counts as
        qualifying, the same way `.min_confidence()` does. Each principal's
        own trust_level is always its current value: no code path ever
        changes a principal's trust_level after creation, so there is no
        historical value to reconstruct — "as of t" and "now" are the same
        number by construction, and the same holds for the `min()` this
        method now takes of two such principals' trust levels.

        Args:
            level: Minimum effective trust level, 0-10.

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

    def include_flagged(self) -> "QueryBuilder":
        """Also match `.where()` filters against `flagged` assertions
        (SPEC §11.2, §10.3, KI-081).

        By default a `.where()` filter matches only `active` assertions, so
        an entity whose only matching value sits on a flagged (contradicted)
        assertion is not returned. This opt-in widens the match set to
        `active` + `flagged`. It is a *match-set* widener, not a result-shape
        change — `.all()` still returns `list[Entity]`.

        No effect on a query with no `.where()` filter (nothing to match
        against) or one already scoped by `.as_of()` (a flagged assertion's
        open validity window already makes it visible in a bitemporal
        snapshot unless excluded — the `as_of` path has honored this flag
        since before KI-081).

        Does not yet compose with `.min_confidence()` / `.trust_at_least()`:
        those still consider only `active` assertions, so chaining one after
        an `.include_*()` re-narrows the result to active (KI-093).

        Returns:
            Self for chaining
        """
        self._include_flagged = True
        return self

    def include_history(self) -> "QueryBuilder":
        """Also match `.where()` filters against `superseded` and
        `retracted` assertions (SPEC §11.2, KI-081).

        By default a `.where()` filter matches only `active` assertions.
        This opt-in widens the current-state match set to also include
        assertions a later write superseded or a `retract()` withdrew — so
        `kb.query(Person).where(name="Ada").include_history()` returns a
        person whose name *was* "Ada" even if it isn't now. Combine with
        `.include_flagged()` to widen to every status.

        Like `.include_flagged()`, a match-set widener, not a result-shape
        change: `.all()` returns `list[Entity]`, with no per-entity
        timeline attached. No effect on a query with no `.where()` filter,
        or one scoped by `.as_of()` (a bitemporal snapshot already matches
        whatever assertion was valid at that instant, regardless of its
        status now). Does not yet compose with `.min_confidence()` /
        `.trust_at_least()` (KI-093 — those still consider `active` only).

        Returns:
            Self for chaining
        """
        self._include_history = True
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
            include_flagged=self._include_flagged,
            include_history=self._include_history,
        )

    def _qualified_filters(self) -> list[tuple[str, str, Any]]:
        """Push .where() filters to the backend as `(full_predicate, operator,
        value)` triples: `("name", "eq")` -> `("Concept.name", "eq", ...)`.

        A list, not a dict (KI-039): two different operators can target the
        same predicate (e.g. `age__gte` and `age__lt`), which a
        predicate-keyed dict couldn't represent.
        """
        return [
            (f"{self._concept}.{prop}", operator, value)
            for (prop, operator), value in self._filters.items()
        ]

    def _semantic_candidates(self) -> list[Entity]:
        """Vector-search-first ranking, optionally intersected with .where().

        Overfetches from the vector index, then (if .where() is also set)
        intersects with the symbolic matches while preserving vector rank
        order — a true symbolic match ranked below the overfetch window in
        the global vector ranking is missed (see the module-level comment).

        Raises:
            ValidationError: No Embedder was configured on this QueryBuilder.

        Note (KI-058, found exposing `.as_of()` through MCP for the first
        time — no prior interface combined the two): this only closes the
        "entity didn't exist yet at `as_of_time`" gap, via the same
        `created_at <= as_of_time` check `.entities()` already applies. It
        does NOT make semantic search itself bitemporal — the vector index
        holds one embedding per entity, generated at whatever point
        `Ontology.reindex()` was last called, with no historical versions.
        `.semantic()` combined with `.as_of()` therefore always ranks by
        the entity's *current* embedded content, never its content as it
        stood at `as_of_time` — a structural limitation of the vector
        index having no temporal dimension, not something this method can
        patch around.

        A related ceiling for `.semantic().where(...).include_flagged()` /
        `.include_history()` (KI-081): `Ontology.reindex()` embeds only
        *active* Text values and skips an entity with none, so an entity
        whose entire matching content is flagged/superseded/retracted is
        absent from the vector index and unreachable this way even though
        the pure-symbolic path (`_base_candidates`) would return it.
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
                    include_flagged=self._include_flagged,
                    include_history=self._include_history,
                )
            }
            ranked_ids = [entity_id for entity_id in ranked_ids if entity_id in symbolic_ids]

        # No .where() to intersect with: the as_of-existence check ("did
        # this entity exist yet at as_of_time?") that entities_where() would
        # otherwise apply happens below instead, per-candidate against the
        # entity row the loop already fetches — not via a second full-concept
        # scan, which would cost O(concept size) per call regardless of the
        # (already-bounded) candidate count (KI-058 review, round 2: an
        # earlier version here did do a full entities(as_of_time=...) scan
        # and measured a 25x regression at 20k entities).
        entities = []
        for entity_id in ranked_ids:
            entity = self._backend.get_entity(entity_id)
            if (
                entity is not None
                and entity.namespace == self._namespace
                and entity.concept == self._concept
                and (self._as_of_time is None or entity.created_at <= self._as_of_time)
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

        When `.where()`/`.semantic()` already narrowed `entities` below the
        full concept, and that narrowed set is no larger than
        `_CANDIDATE_HINT_MAX`, the narrowed id set is also passed down as an
        optional `candidate_ids` hint (KI-037) — a backend MAY use it to
        scope the scan further (SQLite does); this is always safe even if
        ignored, since the result is re-intersected against `entities`
        below regardless. Not passed when `entities` *is* the full concept
        (no `.where()`/`.semantic()`, where the hint carries no benefit and
        SQLite would pay to encode it for nothing) or when it's narrowed but
        still large (a low-selectivity `.where()` predicate can match
        thousands of entities — past `_CANDIDATE_HINT_MAX`, encoding the
        hint costs more than the scan it would save; see that constant's
        docstring for the measured crossover).
        """
        if self._min_confidence is None and self._trust_at_least is None:
            return entities
        if not entities:
            return entities

        candidate_ids: frozenset[str] | None = None
        if (self._filters or self._semantic_text is not None) and len(
            entities
        ) <= _CANDIDATE_HINT_MAX:
            candidate_ids = frozenset(e.id for e in entities)

        qualifying_ids: set[str] | None = None

        if self._min_confidence is not None:
            qualifying_ids = self._backend.entities_meeting_confidence(
                self._namespace,
                self._concept,
                self._min_confidence,
                as_of_time=self._as_of_time,
                candidate_ids=candidate_ids,
            )

        if self._trust_at_least is not None:
            trust_ids = self._backend.entities_meeting_trust(
                self._namespace,
                self._concept,
                self._trust_at_least,
                as_of_time=self._as_of_time,
                candidate_ids=candidate_ids,
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
