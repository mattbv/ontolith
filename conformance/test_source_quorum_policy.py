"""Conformance vector: SourceQuorum's kb parameter (SPEC §9.2, KI-017, ADR-0025).

SourceQuorum is the first PolicyStrategy that actually inspects KB state —
these vectors prove `evaluate()`'s `kb` argument sees real, backend-persisted
assertions through the `AsOfView` snapshot Ontology constructs at each of the
three governed write paths (propose, propose_ref, retract), across both
backends via `make_kb`/`backend_name`.

Corroborating assertions are seeded via `assert_literal`/`assert_ref` (SPEC
§9.3 direct writes) rather than through SourceQuorum itself, since a
RequireReview decision never persists an assertion — only already-committed
facts can corroborate a later proposal.
"""

from __future__ import annotations

from datetime import UTC, datetime

from conformance.conftest import KbFactory
from ontolith import Entity, Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.govern.policy import AutoAccept, RequireReview, SourceQuorum

T0 = datetime(2025, 1, 1, tzinfo=UTC)
AUTHOR = "alice@example.com"
DELEGATE = "delegate@example.com"


def _seeded_kb(make_kb: KbFactory, id_tail: list[str]) -> tuple[Ontology, Entity]:
    """kb with AUTHOR (write capability) and a Person entity, ready to seed assertions."""
    kb = make_kb(
        FixedClock(T0),
        FixedIdProvider(["e-1", *id_tail]),
        policy=SourceQuorum(threshold=2),
    )
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author=AUTHOR)
    return kb, entity


def test_quorum_reached_via_seeded_corroboration_auto_accepts(make_kb: KbFactory) -> None:
    """A second distinct source pushes a matching-value proposal over threshold=2."""
    kb, entity = _seeded_kb(make_kb, ["a-seed", "a-1", "prop-1"])
    kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-a")

    proposal, decision = kb.propose(
        entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-b"
    )

    assert isinstance(decision, AutoAccept)
    assert "2/2" in decision.reason
    assert proposal.state == "auto_accepted"
    values = {a.value for a in kb.assertions(subject=entity.id, predicate="Person.name")}
    assert values == {"Ada"}


def test_quorum_not_met_requires_review(make_kb: KbFactory) -> None:
    """threshold=3 with only 2 distinct sources (1 seeded + proposal's own) requires review."""
    kb = make_kb(
        FixedClock(T0),
        FixedIdProvider(["e-1", "a-seed", "prop-1"]),
        policy=SourceQuorum(threshold=3),
    )
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author=AUTHOR)
    kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-a")

    proposal, decision = kb.propose(
        entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-b"
    )

    assert isinstance(decision, RequireReview)
    assert "2/3" in decision.reason
    assert proposal.state == "require_review"


def test_retraction_always_requires_review(make_kb: KbFactory) -> None:
    """Retractions never auto-accept under SourceQuorum, even at threshold=1."""
    kb = make_kb(
        FixedClock(T0),
        FixedIdProvider(["e-1", "a-1", "prop-1"]),
        policy=SourceQuorum(threshold=1),
    )
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author=AUTHOR)
    assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, source="s")

    proposal, decision = kb.retract(assertion.id, AUTHOR)

    assert isinstance(decision, RequireReview)
    assert "retract" in decision.reason.lower()
    assert kb.assertions(subject=entity.id, predicate="Person.name", status="active") != []


def test_sourceless_proposal_requires_review(make_kb: KbFactory) -> None:
    """A proposal with no source can't establish a quorum, regardless of corroboration."""
    kb, entity = _seeded_kb(make_kb, ["a-seed", "a-1", "prop-1"])
    kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-a")
    kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-b")

    proposal, decision = kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR, source=None)

    assert isinstance(decision, RequireReview)
    assert "no source" in decision.reason.lower()
    assert proposal.state == "require_review"


def test_mismatched_value_does_not_corroborate(make_kb: KbFactory) -> None:
    """An existing assertion with a different value doesn't count toward quorum."""
    kb, entity = _seeded_kb(make_kb, ["a-seed", "prop-1"])
    kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-a")

    proposal, decision = kb.propose(
        entity.id, "Person.name", "Adaeze", "Text", AUTHOR, source="source-b"
    )

    assert isinstance(decision, RequireReview)
    assert "1/2" in decision.reason


def test_sourceless_existing_assertion_does_not_corroborate(make_kb: KbFactory) -> None:
    """An existing assertion with no recorded source can't count toward quorum."""
    kb, entity = _seeded_kb(make_kb, ["a-seed", "prop-1"])
    kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, source=None)

    proposal, decision = kb.propose(
        entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-b"
    )

    assert isinstance(decision, RequireReview)
    assert "1/2" in decision.reason


def test_propose_ref_target_based_corroboration(make_kb: KbFactory) -> None:
    """SourceQuorum reads `target` (not `value`) for assert_ref-shaped operations."""
    kb = make_kb(
        FixedClock(T0),
        FixedIdProvider(["e-1", "e-2", "a-seed", "a-1", "prop-1"]),
        policy=SourceQuorum(threshold=2),
    )
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    person = kb.create_entity("Person", author=AUTHOR)
    org = kb.create_entity("Organization", author=AUTHOR)
    kb.assert_ref(person.id, "Person.employer", org.id, AUTHOR, source="source-a")

    proposal, decision = kb.propose_ref(
        person.id, "Person.employer", org.id, AUTHOR, source="source-b"
    )

    assert isinstance(decision, AutoAccept)
    assert proposal.state == "auto_accepted"


def test_delegated_proposal_still_evaluates_quorum(make_kb: KbFactory) -> None:
    """acting_as delegation doesn't bypass or break the kb-inspecting quorum check."""
    kb, entity = _seeded_kb(make_kb, ["a-seed", "a-1", "prop-1"])
    kb.create_principal(
        DELEGATE,
        kind="human",
        auth_method="oidc",
        default_capability="propose",
        owner=AUTHOR,
    )
    kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-a")

    proposal, decision = kb.propose(
        entity.id,
        "Person.name",
        "Ada",
        "Text",
        DELEGATE,
        source="source-b",
        acting_as=AUTHOR,
    )

    assert isinstance(decision, AutoAccept)
    assert proposal.state == "auto_accepted"
