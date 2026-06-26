"""Traversal and write performance benchmarks — M1 baseline.

Budgets (enforced M4, baselined here per implementation plan §9):
  - Single-entity get with provenance:  p95 < 10 ms
  - propose + policy eval + commit:     p95 < 50 ms
  - 3-hop traversal, 100k-assertion KB: p95 < 200 ms
  - Symbolic query (concept + filter):  p95 < 150 ms

These benchmarks are informational in M1 (no budget gate).
Run with: uv run pytest tests/benchmarks/ --benchmark-only
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import Assertion, Entity
from ontolith.identity import Principal
from ontolith.store.sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

AUTHOR_ID = "bench@example.com"
T0 = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def seeded_db_path() -> Path:
    """One-time setup: SQLite DB seeded with 100k assertions across 1k entities.

    Uses the backend directly to avoid ID-provider overhead during seeding.
    Each entity has 100 assertions (one predicate per index).
    """
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = Path(f.name)
    f.close()

    backend = SQLiteBackend(path)
    backend.put_principal(Principal(id=AUTHOR_ID, kind="human", auth_method="oidc", created_at=T0))

    # 1 000 Person entities, each with 100 literal assertions = 100 000 assertions.
    num_entities = 1_000
    assertions_per_entity = 100

    with backend.transaction():
        for i in range(num_entities):
            eid = f"entity-{i:06d}"
            backend.put_entity(
                Entity(
                    id=eid,
                    namespace="default",
                    concept="Person",
                    created_at=T0,
                    created_by=AUTHOR_ID,
                )
            )
        for i in range(num_entities):
            eid = f"entity-{i:06d}"
            for j in range(assertions_per_entity):
                backend.put_assertion(
                    Assertion(
                        id=f"assertion-{i:06d}-{j:03d}",
                        namespace="default",
                        subject=eid,
                        predicate=f"Person.attr{j:03d}",
                        value_kind="literal",
                        value_type="Text",
                        value=f"value-{i}-{j}",
                        author=AUTHOR_ID,
                        asserted_at=T0,
                    )
                )

    # Wire 2-hop ref chain: entity-0 → entity-1 → entity-2 (for traversal bench)
    with backend.transaction():
        backend.put_assertion(
            Assertion(
                id="ref-hop-0",
                namespace="default",
                subject="entity-000000",
                predicate="Person.knows",
                value_kind="ref",
                value="entity-000001",
                author=AUTHOR_ID,
                asserted_at=T0,
            )
        )
        backend.put_assertion(
            Assertion(
                id="ref-hop-1",
                namespace="default",
                subject="entity-000001",
                predicate="Person.knows",
                value_kind="ref",
                value="entity-000002",
                author=AUTHOR_ID,
                asserted_at=T0,
            )
        )

    backend.close()
    yield path
    path.unlink()


@pytest.fixture(scope="module")
def seeded_backend(seeded_db_path: Path) -> SQLiteBackend:
    backend = SQLiteBackend(seeded_db_path)
    yield backend
    backend.close()


@pytest.fixture(scope="module")
def seeded_kb(seeded_db_path: Path) -> Ontology:
    kb = Ontology.connect(seeded_db_path)
    yield kb
    kb.close()


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def test_bench_single_entity_get(benchmark, seeded_backend: SQLiteBackend) -> None:
    """p95 target: < 10 ms — Single entity retrieval by ID."""
    result = benchmark(seeded_backend.get_entity, "entity-000500")
    assert result is not None
    assert result.id == "entity-000500"


def test_bench_single_entity_provenance(benchmark, seeded_backend: SQLiteBackend) -> None:
    """p95 target: < 10 ms — Entity retrieval + all assertions (provenance)."""

    def get_with_provenance() -> tuple:
        entity = seeded_backend.get_entity("entity-000500")
        assertions = seeded_backend.assertions(subject="entity-000500")
        return entity, assertions

    entity, assertions = benchmark(get_with_provenance)
    assert entity is not None
    assert len(assertions) == 100


def test_bench_write_assert_literal(benchmark, seeded_kb: Ontology) -> None:
    """p95 target: < 50 ms — Write path: assert_literal (standalone commit)."""
    counter = {"n": 0}

    def write_one() -> None:
        n = counter["n"]
        counter["n"] += 1
        seeded_kb.assert_literal(
            "entity-000999",
            "Person.bench_write",
            f"value-{n}",
            "Text",
            AUTHOR_ID,
        )

    benchmark(write_one)


def test_bench_symbolic_query_concept_filter(benchmark, seeded_kb: Ontology) -> None:
    """p95 target: < 150 ms — Symbolic query: concept + attribute filter."""

    def query() -> list:
        return seeded_kb.query("Person").where(attr000="value-0-0").all()

    results = benchmark(query)
    assert len(results) >= 1


def test_bench_query_all_entities_of_concept(benchmark, seeded_backend: SQLiteBackend) -> None:
    """Scan all entities of a concept in a 100k-assertion KB."""

    def query() -> list:
        return seeded_backend.entities(namespace="default", concept="Person")

    results = benchmark(query)
    assert len(results) == 1_000


def test_bench_2hop_traversal(benchmark, seeded_backend: SQLiteBackend) -> None:
    """p95 target: < 200 ms (3-hop budget) — 2-hop ref traversal via assertions.

    M1 has no dedicated traversal API; this manually chains assertion lookups.
    A proper graph traversal query is a M2+ feature.
    """

    def traverse_2hop() -> list[str]:
        # Hop 1: entity-000000 → knows → entity-000001
        hop1 = seeded_backend.assertions(subject="entity-000000", predicate="Person.knows")
        if not hop1:
            return []
        mid_id = hop1[0].value

        # Hop 2: entity-000001 → knows → entity-000002
        hop2 = seeded_backend.assertions(subject=mid_id, predicate="Person.knows")
        return [a.value for a in hop2]

    result = benchmark(traverse_2hop)
    assert result == ["entity-000002"]


def test_bench_assertions_by_subject(benchmark, seeded_backend: SQLiteBackend) -> None:
    """Retrieve all 100 assertions for a single entity."""
    results = benchmark(seeded_backend.assertions, subject="entity-000500")
    assert len(results) == 100
