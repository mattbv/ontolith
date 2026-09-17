"""Traversal and write performance benchmarks — M1 baseline, M4 write-path fix.

Budgets (enforced M4, baselined here per implementation plan §9):
  - Single-entity get with provenance:  p95 < 10 ms
  - propose + policy eval + commit:     p95 < 50 ms
  - 3-hop traversal, 100k-assertion KB: p95 < 200 ms
  - Symbolic query (concept + filter):  p95 < 150 ms
  - as_of(t) reconstruction, 100k-assertion KB: p95 < 300 ms

These benchmarks are informational in M1 (no budget gate).
Run with: uv run pytest tests/benchmarks/ --benchmark-only

M4 note: `test_bench_write_assert_literal` used to call `assert_literal` on the
*same* (subject, predicate) every round with a different value each time. With
no schema registered, that predicate defaults to static/single cardinality —
so the 2nd round already contradicts the 1st, and every round after that
extends the same growing open contradiction (`_apply_with_conflict_routing`'s
"extend an already-open contradiction" fast path scans every existing member).
Reproduced directly: the open contradiction's `member_ids` genuinely grows one
entry per round. The reported numbers were an artifact of how many rounds
pytest-benchmark happened to calibrate for a given run, not a stable per-write
cost — fixed by giving each round its own (subject, predicate), which has
nothing to contradict. `test_bench_propose_auto_accept` is new: the budget row
itself names "propose + policy eval + commit," a materially different, more
expensive path than direct `assert_literal` (proposal construction/persistence
+ `ThresholdPolicy.evaluate()`), which had no benchmark of its own before.
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
    backend.put_principal(
        Principal(
            id=AUTHOR_ID,
            kind="human",
            auth_method="oidc",
            default_capability="write",
            created_at=T0,
        )
    )

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

    # A small, dedicated pool of write-target entities under their own
    # concept (WriteBenchTarget, not Person) — write benchmarks below cycle
    # through these exclusively, never the Person pool above. Keeps every
    # write benchmark's own state from leaking into a read benchmark's
    # exact-count assertion elsewhere in this module-scoped fixture (e.g.
    # test_bench_assertions_by_subject's == 100, test_bench_query_all_
    # entities_of_concept's == 1_000) regardless of pytest-benchmark's own
    # per-run round count or test execution order.
    num_write_bench_entities = 100
    with backend.transaction():
        for i in range(num_write_bench_entities):
            backend.put_entity(
                Entity(
                    id=f"write-bench-entity-{i:04d}",
                    namespace="default",
                    concept="WriteBenchTarget",
                    created_at=T0,
                    created_by=AUTHOR_ID,
                )
            )

    # Wire 3-hop ref chain: entity-0 → entity-1 → entity-2 → entity-3
    # (matches the 3-hop traversal budget in the module docstring).
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
        backend.put_assertion(
            Assertion(
                id="ref-hop-2",
                namespace="default",
                subject="entity-000002",
                predicate="Person.knows",
                value_kind="ref",
                value="entity-000003",
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


@pytest.mark.benchmark
def test_bench_single_entity_get(benchmark, seeded_backend: SQLiteBackend) -> None:
    """p95 target: < 10 ms — Single entity retrieval by ID."""
    result = benchmark(seeded_backend.get_entity, "entity-000500")
    assert result is not None
    assert result.id == "entity-000500"


@pytest.mark.benchmark
def test_bench_single_entity_provenance(benchmark, seeded_kb: Ontology) -> None:
    """p95 target: < 10 ms — SPEC §5.4's one-call provenance view.

    `Ontology.provenance()` (KI-086/ADR-0047) does three backend
    round-trips: `get_assertion` + `get_proposal_events` +
    `get_assertion_events_by_successor`. A seeded assertion is a direct
    write with no proposal and no supersession, so the latter two return
    empty — the common case this budget targets.
    """
    result = benchmark(seeded_kb.provenance, "assertion-000500-050")
    assert result.assertion.id == "assertion-000500-050"
    assert result.review_events == ()
    assert result.superseded_ids == ()


@pytest.mark.benchmark
def test_bench_write_assert_literal(benchmark, seeded_kb: Ontology) -> None:
    """p95 target: < 50 ms — Write path: assert_literal (standalone commit).

    Each round gets its own (subject, predicate) — cycling through the
    dedicated write-bench entity pool (never the read-benchmarked Person
    pool, see the seeding fixture's own comment), a fresh predicate name
    every round — so there is never anything for conflict routing to
    contradict or supersede against. That's deliberate: this benchmark
    measures a single write's own cost, not the separate (and unbounded)
    cost of extending a growing contradiction — see the module docstring
    for how the previous version of this benchmark conflated the two.
    """
    counter = {"n": 0}

    def write_one() -> None:
        n = counter["n"]
        counter["n"] += 1
        eid = f"write-bench-entity-{n % 100:04d}"
        seeded_kb.assert_literal(
            eid,
            f"WriteBenchTarget.bench_write_{n}",
            f"value-{n}",
            "Text",
            AUTHOR_ID,
        )

    benchmark(write_one)


@pytest.mark.benchmark
def test_bench_propose_auto_accept(benchmark, seeded_kb: Ontology) -> None:
    """p95 target: < 50 ms — the budget's own named operation: propose() +
    ThresholdPolicy.evaluate() + commit, not just assert_literal's direct-
    write path. AUTHOR_ID is human with write capability, so ThresholdPolicy
    auto-accepts every round — the write actually lands, exercising the
    same commit path assert_literal does, plus proposal construction/
    persistence and policy evaluation on top. Same fresh-(subject,
    predicate)-per-round shape as test_bench_write_assert_literal, for the
    same reason, and the same dedicated write-bench entity pool."""
    counter = {"n": 0}

    def propose_one() -> None:
        n = counter["n"]
        counter["n"] += 1
        eid = f"write-bench-entity-{n % 100:04d}"
        proposal, decision = seeded_kb.propose(
            eid,
            f"WriteBenchTarget.bench_propose_{n}",
            f"value-{n}",
            "Text",
            AUTHOR_ID,
        )
        assert decision.__class__.__name__ == "AutoAccept"

    benchmark(propose_one)


@pytest.mark.benchmark
def test_bench_symbolic_query_concept_filter(benchmark, seeded_kb: Ontology) -> None:
    """p95 target: < 150 ms — Symbolic query: concept + attribute filter."""

    def query() -> list:
        return seeded_kb.query("Person").where(attr000="value-0-0").all()

    results = benchmark(query)
    assert len(results) >= 1


@pytest.mark.benchmark
def test_bench_query_all_entities_of_concept(benchmark, seeded_backend: SQLiteBackend) -> None:
    """Scan all entities of a concept in a 100k-assertion KB."""

    def query() -> list:
        return seeded_backend.entities(namespace="default", concept="Person")

    results = benchmark(query)
    assert len(results) == 1_000


@pytest.mark.benchmark
def test_bench_3hop_traversal(benchmark, seeded_backend: SQLiteBackend) -> None:
    """p95 target: < 200 ms — 3-hop ref traversal via chained assertion lookups.

    M1 has no dedicated graph traversal API; this manually chains
    `assertions(subject=, predicate=)` calls, one per hop, which is the only
    traversal path available to callers today. A proper traversal query
    (single call, N hops) is a future feature — see docs/known-issues.md.
    """

    def traverse_3hop() -> list[str]:
        current = "entity-000000"
        visited: list[str] = []
        for _ in range(3):
            hop = seeded_backend.assertions(subject=current, predicate="Person.knows")
            if not hop:
                break
            current = str(hop[0].value)
            visited.append(current)
        return visited

    result = benchmark(traverse_3hop)
    assert result == ["entity-000001", "entity-000002", "entity-000003"]


@pytest.mark.benchmark
def test_bench_assertions_by_subject(benchmark, seeded_backend: SQLiteBackend) -> None:
    """Retrieve all 100 assertions for a single entity."""
    results = benchmark(seeded_backend.assertions, subject="entity-000500")
    assert len(results) == 100


@pytest.mark.benchmark
def test_bench_as_of_reconstruction(benchmark, seeded_kb: Ontology) -> None:
    """p95 target: < 300 ms — as_of(t) point-in-time reconstruction over a
    100k-assertion KB (all assertions predate t, so this exercises full
    bitemporal filtering rather than an empty-result fast path)."""
    t = datetime(2025, 6, 1, tzinfo=UTC)

    def query() -> list:
        return seeded_kb.as_of(t).assertions(subject="entity-000500")

    results = benchmark(query)
    assert len(results) == 100
