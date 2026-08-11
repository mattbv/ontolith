"""Hybrid retrieval (semantic search) and confidence/trust filter benchmarks — M3 baseline.

Budget (informational, enforced M4, per implementation plan §9):
  - Hybrid query (vector search, k=10): p95 < 150 ms

.min_confidence()/.trust_at_least() (KI-028) have no dedicated budget row in
the implementation plan; benchmarked here anyway since they previously
regressed into the same N+1 pattern KI-001 fixed for .where(), and the
regression was invisible to CI precisely because nothing benchmarked them.

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


@pytest.fixture(scope="module")
def seeded_confidence_trust_db_path() -> Path:
    """1k Person entities, half authored by a trusted/high-confidence
    principal and half by an untrusted/low-confidence one (KI-028) — no
    .where()/.semantic() narrows the candidate set, so .min_confidence()/
    .trust_at_least() see the full concept scan, matching the scenario
    that exposed the original N+1 pattern."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = Path(f.name)
    f.close()

    backend = SQLiteBackend(path)
    trusted = "trusted@example.com"
    untrusted = "untrusted@example.com"
    backend.put_principal(
        Principal(
            id=trusted,
            kind="human",
            auth_method="oidc",
            default_capability="write",
            trust_level=8,
            created_at=T0,
        )
    )
    backend.put_principal(
        Principal(
            id=untrusted,
            kind="human",
            auth_method="oidc",
            default_capability="write",
            trust_level=1,
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
                    created_by=trusted,
                )
            )
        for i in range(num_entities):
            high_quality = i % 2 == 0
            backend.put_assertion(
                Assertion(
                    id=f"assertion-name-{i:06d}",
                    namespace="default",
                    subject=f"entity-{i:06d}",
                    predicate="Person.name",
                    value_kind="literal",
                    value_type="Text",
                    value=f"Person {i}",
                    author=trusted if high_quality else untrusted,
                    confidence=0.9 if high_quality else 0.2,
                    asserted_at=T0,
                )
            )
    backend.close()

    yield path
    path.unlink()


@pytest.fixture(scope="module")
def seeded_confidence_trust_kb(seeded_confidence_trust_db_path: Path) -> Ontology:
    kb = Ontology.connect(seeded_confidence_trust_db_path)
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


@pytest.mark.benchmark
def test_bench_min_confidence_full_concept_scan(
    benchmark, seeded_confidence_trust_kb: Ontology
) -> None:
    """No .where()/.semantic() - full 1k-entity concept scan filtered by
    .min_confidence() alone (KI-028: previously one assertions() round trip
    per candidate entity)."""

    def query() -> list:
        return seeded_confidence_trust_kb.query("Person").min_confidence(0.5).all()

    results = benchmark(query)
    assert len(results) == 500


@pytest.mark.benchmark
def test_bench_trust_at_least_full_concept_scan(
    benchmark, seeded_confidence_trust_kb: Ontology
) -> None:
    """No .where()/.semantic() - full 1k-entity concept scan filtered by
    .trust_at_least() alone (KI-028: previously one assertions() +
    get_principal() round trip per candidate entity)."""

    def query() -> list:
        return seeded_confidence_trust_kb.query("Person").trust_at_least(5).all()

    results = benchmark(query)
    assert len(results) == 500


@pytest.mark.benchmark
def test_bench_min_confidence_narrowed_by_where(
    benchmark, seeded_confidence_trust_kb: Ontology
) -> None:
    """.where() narrows to a single candidate, then .min_confidence() scopes
    its scan to that candidate via candidate_ids (KI-037) rather than the
    full concept (KI-028's original fix note: the (namespace, concept)
    -scoped design didn't exploit an already-narrow candidate set the way
    the reverted id-list design would have). At this 1k-entity scale the
    win isn't visible — SQLite's query planner already picks an
    index-backed scan for the unnarrowed case too — see
    `test_bench_min_confidence_narrowed_by_where_50k` for a scale where it
    is."""

    def query() -> list:
        return (
            seeded_confidence_trust_kb.query("Person")
            .where(name="Person 500")
            .min_confidence(0.5)
            .all()
        )

    results = benchmark(query)
    assert len(results) == 1


@pytest.fixture(scope="module")
def seeded_confidence_trust_50k_db_path() -> Path:
    """50k Person entities, same shape as `seeded_confidence_trust_db_path`
    but at the scale KI-037's fix note says the candidate_ids narrowing win
    actually becomes visible (the 1k fixture's cost is small enough that
    SQLite's planner already picks an index-backed scan either way)."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = Path(f.name)
    f.close()

    backend = SQLiteBackend(path)
    trusted = "trusted@example.com"
    untrusted = "untrusted@example.com"
    backend.put_principal(
        Principal(
            id=trusted,
            kind="human",
            auth_method="oidc",
            default_capability="write",
            trust_level=8,
            created_at=T0,
        )
    )
    backend.put_principal(
        Principal(
            id=untrusted,
            kind="human",
            auth_method="oidc",
            default_capability="write",
            trust_level=1,
            created_at=T0,
        )
    )

    num_entities = 50_000
    with backend.transaction():
        for i in range(num_entities):
            backend.put_entity(
                Entity(
                    id=f"entity-{i:06d}",
                    namespace="default",
                    concept="Person",
                    created_at=T0,
                    created_by=trusted,
                )
            )
        for i in range(num_entities):
            high_quality = i % 2 == 0
            backend.put_assertion(
                Assertion(
                    id=f"assertion-name-{i:06d}",
                    namespace="default",
                    subject=f"entity-{i:06d}",
                    predicate="Person.name",
                    value_kind="literal",
                    value_type="Text",
                    value=f"Person {i}",
                    author=trusted if high_quality else untrusted,
                    confidence=0.9 if high_quality else 0.2,
                    asserted_at=T0,
                )
            )
    backend.close()

    yield path
    path.unlink()


@pytest.fixture(scope="module")
def seeded_confidence_trust_50k_kb(seeded_confidence_trust_50k_db_path: Path) -> Ontology:
    kb = Ontology.connect(seeded_confidence_trust_50k_db_path)
    yield kb
    kb.close()


@pytest.mark.benchmark
def test_bench_min_confidence_full_concept_scan_50k(
    benchmark, seeded_confidence_trust_50k_kb: Ontology
) -> None:
    """No .where()/.semantic() at 50k entities — the direct comparison
    baseline for `test_bench_min_confidence_narrowed_by_where_50k` below,
    run in the same session/table so the win is visible without needing
    `--benchmark-compare` against a separate historical run."""

    def query() -> list:
        return seeded_confidence_trust_50k_kb.query("Person").min_confidence(0.5).all()

    results = benchmark(query)
    assert len(results) == 25_000


@pytest.mark.benchmark
def test_bench_min_confidence_narrowed_by_where_50k(
    benchmark, seeded_confidence_trust_50k_kb: Ontology
) -> None:
    """Same query as `test_bench_min_confidence_narrowed_by_where`, at 50k
    entities instead of 1k — the scale KI-037's own Fix text says is needed
    to make the candidate_ids narrowing win visible: run alongside
    `test_bench_min_confidence_full_concept_scan_50k`, this measured ~17x
    faster (median 12.4ms vs 212.3ms) in the same benchmark session. Not
    O(1): `EXPLAIN QUERY PLAN` shows SQLite still `SEARCH`es the full
    `(namespace, concept)` range of the `entity` index before
    bloom-filtering against `candidate_ids` — only the assertion-side join
    is pruned to the candidate set, not the entity-side scan. A tighter
    bound would need a covering index keyed to make the candidate lookup
    itself the seek, which is out of scope here."""

    def query() -> list:
        return (
            seeded_confidence_trust_50k_kb.query("Person")
            .where(name="Person 25000")
            .min_confidence(0.5)
            .all()
        )

    results = benchmark(query)
    assert len(results) == 1
