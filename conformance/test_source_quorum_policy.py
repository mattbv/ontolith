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

from datetime import UTC, datetime, timedelta

from conformance.conftest import KbFactory
from ontolith import Entity, Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.govern.policy import AutoAccept, Reject, RequireReview, SourceQuorum

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
    kb, entity = _seeded_kb(make_kb, ["a-seed", "prop-1", "a-new"])
    kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-a")

    proposal, decision = kb.propose(
        entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-b"
    )

    assert isinstance(decision, AutoAccept)
    assert "2/2" in decision.reason
    assert proposal.state == "auto_accepted"
    # Corroborating assertions are kept separate, never merged (SPEC §10.1) —
    # a set-equality check on `.value` alone would also pass with just 1.
    active = kb.assertions(subject=entity.id, predicate="Person.name")
    assert len(active) == 2
    assert {a.source for a in active} == {"source-a", "source-b"}


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
        FixedIdProvider(["e-1", "e-2", "a-seed", "prop-1", "a-new"]),
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


def test_read_capability_principal_rejected(make_kb: KbFactory) -> None:
    """SourceQuorum rejects read-capability principals itself (KI-015 update,
    ADR-0025) - propose()/propose_ref()/retract() have no capability
    pre-check of their own, so this floor is only as strong as the
    installed policy; previously only ThresholdPolicy enforced it."""
    kb = make_kb(
        FixedClock(T0),
        FixedIdProvider(["e-1", "a-seed", "prop-1"]),
        policy=SourceQuorum(threshold=1),
    )
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author=AUTHOR)
    kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-a")
    reader = "reader@example.com"
    kb.create_principal(reader, kind="human", auth_method="oidc", default_capability="read")

    proposal, decision = kb.propose(
        entity.id, "Person.name", "Ada", "Text", reader, source="source-b"
    )

    assert isinstance(decision, Reject)
    assert proposal.state == "rejected"
    [active] = kb.assertions(subject=entity.id, predicate="Person.name")
    assert active.source == "source-a"


def test_ai_authored_proposal_can_auto_accept(make_kb: KbFactory) -> None:
    """SourceQuorum does not special-case AI authorship - ADR-0003's "AI
    always requires review" is ThresholdPolicy's own rule, not reimplemented
    here. Pinned explicitly (ADR-0025 §5) so a future change to this is a
    deliberate decision, not an accidental regression."""
    kb = make_kb(
        FixedClock(T0),
        FixedIdProvider(["e-1", "a-seed", "prop-1", "a-new"]),
        policy=SourceQuorum(threshold=2),
    )
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author=AUTHOR)
    kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-a")
    bot = "bot@example.com"
    kb.create_principal(
        bot, kind="ai", auth_method="apikey", owner=AUTHOR, default_capability="propose"
    )

    proposal, decision = kb.propose(
        entity.id, "Person.name", "Ada", "Text", bot, source="source-b", model="test-model-v1"
    )

    assert isinstance(decision, AutoAccept)
    assert proposal.state == "auto_accepted"


def test_different_subject_does_not_corroborate(make_kb: KbFactory) -> None:
    """A matching predicate/value/source on a *different* subject never
    counts toward quorum - proves the kb.assertions() lookup is scoped by
    subject, not just predicate+value (a mutation test with an unscoped
    lookup would otherwise pass every other vector in this file)."""
    kb = make_kb(
        FixedClock(T0),
        FixedIdProvider(["e-1", "e-2", "a-seed", "prop-1"]),
        policy=SourceQuorum(threshold=2),
    )
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    entity_1 = kb.create_entity("Person", author=AUTHOR)
    entity_2 = kb.create_entity("Person", author=AUTHOR)
    kb.assert_literal(entity_1.id, "Person.name", "Ada", "Text", AUTHOR, source="source-a")

    proposal, decision = kb.propose(
        entity_2.id, "Person.name", "Ada", "Text", AUTHOR, source="source-b"
    )

    assert isinstance(decision, RequireReview)
    assert "1/2" in decision.reason


def test_retracted_assertion_does_not_corroborate(make_kb: KbFactory) -> None:
    """A retracted assertion must never count toward quorum. Two mechanisms
    now cooperate to guarantee this (ADR-0049, KI-095): an open-ended
    validity window (this test's shape — no explicit `valid_to`) gets
    closed at the retraction time by `_retraction_valid_to()`, so the
    window check alone already excludes it from the AsOfView snapshot
    SourceQuorum reads; an assertion with an explicit, later `valid_to`
    (see `test_retracted_assertion_with_explicit_valid_to_does_not_corroborate`
    below) relies on the second mechanism instead — `assertions()`'s own
    retraction-event-aware exclusion, since its window alone would not have
    excluded it. Uses the default ThresholdPolicy to perform the retraction
    (SourceQuorum always requires review on retractions, so it can never
    itself accept one), then swaps to SourceQuorum for the corroboration
    check."""
    kb = make_kb(FixedClock(T0), FixedIdProvider(["e-1", "a-seed", "retract-prop", "prop-1"]))
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author=AUTHOR)
    seed = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-a")
    _, retract_decision = kb.retract(seed.id, AUTHOR)
    assert isinstance(retract_decision, AutoAccept)

    kb.policy = SourceQuorum(threshold=2)
    proposal, decision = kb.propose(
        entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-b"
    )

    assert isinstance(decision, RequireReview)
    assert "1/2" in decision.reason


def test_retracted_assertion_with_explicit_valid_to_does_not_corroborate(
    make_kb: KbFactory,
) -> None:
    """ADR-0049 (KI-095)'s actual governance-impact case: an assertion
    asserted with an explicit, far-future `valid_to` has a window
    `_retraction_valid_to()` never narrows at retraction (unlike the
    open-ended window in the test above), so only `assertions()`'s own
    retraction-event-aware exclusion — not the validity window — keeps it
    from corroborating a later proposal. Verified by hand before writing
    this test: reverting that exclusion changes what the second proposal
    below auto-accepts into, a retracted assertion silently counting
    toward quorum — the mutation manifests as a `StorageError` (a spare id
    the fixed provider held for a non-existent case) rather than a clean
    decision-type mismatch, since the auto-accept path this opens draws an
    id the `RequireReview` path never needed; asserted on the decision
    itself below, not on the exception, since a future unrelated change
    to id consumption on the accept path shouldn't make this test's
    failure mode this specific."""
    kb = make_kb(
        FixedClock(T0), FixedIdProvider(["e-1", "a-seed", "retract-prop", "prop-1", "spare-1"])
    )
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author=AUTHOR)
    seed = kb.assert_literal(
        entity.id,
        "Person.name",
        "Ada",
        "Text",
        AUTHOR,
        source="source-a",
        valid_to=T0 + timedelta(days=3650),
    )
    _, retract_decision = kb.retract(seed.id, AUTHOR)
    assert isinstance(retract_decision, AutoAccept)

    kb.policy = SourceQuorum(threshold=2)
    proposal, decision = kb.propose(
        entity.id, "Person.name", "Ada", "Text", AUTHOR, source="source-b"
    )

    assert isinstance(decision, RequireReview)
    assert "1/2" in decision.reason


def test_as_of_replay_at_created_at_sees_same_instant_writes(make_kb: KbFactory) -> None:
    """Corrects an overclaim from an earlier ADR-0025 draft: replaying
    as_of(proposal.created_at) does NOT reproduce the exact evaluation-time
    read if anything else was committed at that same instant afterward -
    `asserted_at <= t` is inclusive of `t`. A same-instant write (trivial
    under FixedClock, as here) IS visible on replay."""
    kb = make_kb(FixedClock(T0), FixedIdProvider(["e-1", "a-1"]))
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author=AUTHOR)
    assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, source="s")

    replay = kb.as_of(T0).assertions(subject=entity.id, predicate="Person.name")

    assert any(a.id == assertion.id for a in replay)


def test_delegated_proposal_still_evaluates_quorum(make_kb: KbFactory) -> None:
    """A delegated (acting_as) proposal still goes through the same kb-inspecting
    quorum check as a non-delegated one - SourceQuorum doesn't read acting_as at
    all, so this only proves the delegation plumbing doesn't interfere."""
    kb, entity = _seeded_kb(make_kb, ["a-seed", "prop-1", "a-new"])
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
