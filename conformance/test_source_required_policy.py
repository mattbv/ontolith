"""Conformance vector: SourceRequired policy strategy (SPEC §9.2, KI-069, ADR-0045).

Deeper edge-case coverage (retractions, unrecognized op kinds, empty-string
sources, delegation, the KI-015 capability floor) lives in
tests/unit/test_govern.py::TestSourceRequired — this file only pins the
SPEC-level contract: pure, deterministic, and the core accept/review split
on whether the proposal's operation carries a source.

Every test above `test_end_to_end_source_is_read_through_kb_propose` hand-
constructs the `operations` payload and calls `evaluate()` directly — the
one `make_kb`-driven test below proves the `source` key this strategy reads
(`op.get("source")`) actually matches what `Ontology.propose()` writes
there (found in review, KI-069) — mirroring how
`conformance/test_source_quorum_policy.py` already does this for
`SourceQuorum`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from conformance.conftest import KbFactory
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.govern.policy import AutoAccept, RequireReview, SourceRequired
from ontolith.govern.proposal import Proposal
from ontolith.identity import Principal

POLICY = SourceRequired(reviewers=["reviewer@example.com"])

T0 = datetime(2025, 1, 1, tzinfo=UTC)

PRINCIPAL = Principal(
    id="author",
    kind="human",
    auth_method="oidc",
    default_capability="propose",
    created_at=T0,
)


def _proposal(source: str | None) -> Proposal:
    return Proposal(
        id="prop-001",
        namespace="default",
        author=PRINCIPAL.id,
        created_at=T0,
        payload={
            "operations": [
                {
                    "kind": "assert_literal",
                    "subject": "e-1",
                    "predicate": "Person.name",
                    "value": "Ada",
                    "value_type": "Text",
                    "source": source,
                }
            ]
        },
    )


def test_source_present_auto_accepts() -> None:
    """A non-empty source auto-accepts (SPEC §9.2)."""
    decision = POLICY.evaluate(_proposal("some-source"), PRINCIPAL)
    assert isinstance(decision, AutoAccept)


def test_missing_source_requires_review() -> None:
    """No source requires review, with the configured reviewers."""
    decision = POLICY.evaluate(_proposal(None), PRINCIPAL)
    assert isinstance(decision, RequireReview)
    assert decision.reviewers == ["reviewer@example.com"]


def test_policy_is_deterministic() -> None:
    """Same inputs always produce the same decision (SPEC §9.2 purity)."""
    d1 = POLICY.evaluate(_proposal("some-source"), PRINCIPAL)
    d2 = POLICY.evaluate(_proposal("some-source"), PRINCIPAL)
    assert type(d1) is type(d2)
    assert d1.reason == d2.reason


def test_policy_is_stateless() -> None:
    """Policy carries no state between evaluations."""
    assert isinstance(POLICY.evaluate(_proposal("s"), PRINCIPAL), AutoAccept)
    assert isinstance(POLICY.evaluate(_proposal(None), PRINCIPAL), RequireReview)
    assert isinstance(POLICY.evaluate(_proposal("s"), PRINCIPAL), AutoAccept)


def test_end_to_end_source_is_read_through_kb_propose(make_kb: KbFactory) -> None:
    """`kb.propose(..., source=...)`'s real payload is what this strategy
    actually reads - proves the `source` key isn't just an assumption
    shared between this file's own fixtures and the strategy."""
    kb = make_kb(
        FixedClock(T0),
        FixedIdProvider(["e-1", "prop-1", "a-1", "prop-2"]),
        policy=SourceRequired(),
    )
    kb.create_principal("author", kind="human", auth_method="oidc", default_capability="propose")
    entity = kb.create_entity("Person", author="author")

    accepted, accept_decision = kb.propose(
        entity.id, "Person.name", "Ada", "Text", "author", source="some-source"
    )
    assert isinstance(accept_decision, AutoAccept)
    assert accepted.state == "auto_accepted"
    [active] = kb.assertions(subject=entity.id, predicate="Person.name")
    assert active.value == "Ada"

    reviewed, review_decision = kb.propose(
        entity.id, "Person.name", "Adaeze", "Text", "author", source=None
    )
    assert isinstance(review_decision, RequireReview)
    assert reviewed.state == "require_review"
