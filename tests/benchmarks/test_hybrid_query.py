"""Hybrid retrieval (semantic search) benchmark — M3 baseline.

Budget (informational, enforced M4, per implementation plan §9):
  - Hybrid query (vector search, k=10): p95 < 150 ms

This benchmark is informational in M3 (no budget gate), matching every
other row in test_traversal.py. Run with:
    uv run pytest tests/benchmarks/test_hybrid_query.py --benchmark-only
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
def seeded_hybrid_db_path() -> Path:
    """One-time setup: 1k Person entities, each with a short Text bio, reindexed once.

    Mirrors test_traversal.py's seeded_db_path fixture-building approach
    (raw backend writes for seeding speed) but with Text-typed assertions so
    Ontology.reindex() has content to embed.
    """
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = Path(f.name)
    f.close()

    backend = SQLiteBackend(path)
    backend.put_principal(
        Principal(
            id=AUTHOR_ID,
            kind="human",
            auth_method="oidc",
            default_capability="write",
            created_at=T0,
        )
    )

    num_entities = 1_000
    with backend.transaction():
        for i in range(num_entities):
            backend.put_entity(
                Entity(
                    id=f"entity-{i:06d}",
                    namespace="default",
                    concept="Person",
                    created_at=T0,
                    created_by=AUTHOR_ID,
                )
            )
        for i in range(num_entities):
            backend.put_assertion(
                Assertion(
                    id=f"assertion-bio-{i:06d}",
                    namespace="default",
                    subject=f"entity-{i:06d}",
                    predicate="Person.bio",
                    value_kind="literal",
                    value_type="Text",
                    value=f"Person number {i} works in research field {i % 37} of the sample dataset",
                    author=AUTHOR_ID,
                    asserted_at=T0,
                )
            )
    backend.close()

    kb = Ontology.connect(path)
    kb.reindex()
    kb.close()

    yield path
    path.unlink()


@pytest.fixture(scope="module")
def seeded_hybrid_kb(seeded_hybrid_db_path: Path) -> Ontology:
    kb = Ontology.connect(seeded_hybrid_db_path)
    yield kb
    kb.close()


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
def test_bench_hybrid_semantic_query_k10(benchmark, seeded_hybrid_kb: Ontology) -> None:
    """p95 target: < 150 ms — .semantic() vector search, k=10, over a 1k-entity index."""

    def query() -> list:
        return (
            seeded_hybrid_kb.query("Person")
            .semantic("research field 12 of the sample dataset")
            .limit(10)
            .all()
        )

    results = benchmark(query)
    assert len(results) == 10


@pytest.mark.benchmark
def test_bench_hybrid_semantic_query_intersected_with_where(
    benchmark, seeded_hybrid_kb: Ontology
) -> None:
    """p95 target: < 150 ms — .semantic() + .where() intersection, k=10."""

    def query() -> list:
        return (
            seeded_hybrid_kb.query("Person")
            .semantic("research field of the sample dataset")
            .where(bio="Person number 500 works in research field 19 of the sample dataset")
            .limit(10)
            .all()
        )

    results = benchmark(query)
    assert len(results) <= 10
