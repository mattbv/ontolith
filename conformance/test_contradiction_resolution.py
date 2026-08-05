"""Conformance vectors for contradiction resolution (SPEC §10.3, §19).

Ontology.resolve_contradiction() lets a reviewer pick a winning assertion
among an open contradiction's flagged members. Losers are retracted, the
winner is reactivated, and the contradiction transitions to 'resolved'.
Resolution is append-only: only status/state fields mutate, nothing is deleted.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.core.errors import AuthError, CapabilityError, NotFoundError, ValidationError

T0 = datetime(2025, 1, 1, tzinfo=UTC)

HUMAN_WRITE = "alice@example.com"
REVIEWER = "bob@example.com"
NON_REVIEWER = "carol@example.com"


def _kb(make_kb: KbFactory) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(30)])
    kb = make_kb(clock, ids)
    kb.create_principal(HUMAN_WRITE, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(REVIEWER, kind="human", auth_method="oidc", default_capability="review")
    kb.create_principal(
        NON_REVIEWER, kind="human", auth_method="oidc", default_capability="propose"
    )
    return kb


def _open_contradiction(kb: Ontology, entity_id: str) -> tuple[str, str, str]:
    """Create a two-member contradiction; returns (contradiction_id, ada_id, ava_id)."""
    kb.propose(entity_id, "Person.name", "Ada", "Text", HUMAN_WRITE)
    kb.propose(entity_id, "Person.name", "Ava", "Text", HUMAN_WRITE)
    flagged = kb.assertions(subject=entity_id, predicate="Person.name", status="flagged")
    ada_id = next(a.id for a in flagged if a.value == "Ada")
    ava_id = next(a.id for a in flagged if a.value == "Ava")
    contradiction = kb.backend.get_open_contradiction("default", entity_id, "Person.name")
    assert contradiction is not None
    return contradiction.id, ada_id, ava_id


# ===========================================================================
# raised_by attribution
# ===========================================================================


class TestContradictionRaisedBy:
    """Contradictions record who raised them, whether auto-detected during
    conflict routing or explicitly flagged via flag_contradiction()."""

    def test_auto_detected_contradiction_records_raiser(self, make_kb: KbFactory) -> None:
        """The author of the assertion whose write triggered conflict-routing
        detection is recorded as raised_by."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, _, _ = _open_contradiction(kb, entity.id)

        contradiction = kb.backend.get_contradiction(contradiction_id)
        assert contradiction is not None
        assert contradiction.raised_by == HUMAN_WRITE

    def test_explicit_flag_records_flagging_principal(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.assert_literal(entity.id, "Person.born", "1815", "Text", HUMAN_WRITE)
        kb.create_principal(
            "dave@example.com", kind="human", auth_method="oidc", default_capability="propose"
        )
        a = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)
        b = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)

        contradiction, _ = kb.flag_contradiction(a.id, b.id, "dave@example.com")

        fetched = kb.backend.get_contradiction(contradiction.id)
        assert fetched is not None
        assert fetched.raised_by == "dave@example.com"


# ===========================================================================
# Successful resolution
# ===========================================================================


class TestResolveContradiction:
    def test_third_party_reviewer_can_resolve(self, make_kb: KbFactory) -> None:
        """REVIEWER, party to neither disputed value, can resolve normally -
        pins that KI-026's self-resolution guard doesn't block legitimate
        resolutions, rather than relying implicitly on every other test in
        this class using a reviewer unrelated to both members."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        resolved = kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        assert resolved.state == "resolved"

    def test_resolve_sets_contradiction_state(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        resolved = kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        assert resolved.state == "resolved"

    def test_resolve_sets_resolved_by_and_resolved_at(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        resolved = kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        assert resolved.resolved_by == REVIEWER
        assert resolved.resolved_at is not None

    def test_winner_reactivated(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].id == ada_id
        assert active[0].value == "Ada"

    def test_losers_retracted(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        retracted = kb.assertions(subject=entity.id, predicate="Person.name", status="retracted")
        assert len(retracted) == 1
        assert retracted[0].id == ava_id

    def test_losers_no_longer_flagged(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert flagged == []

    def test_resolution_is_append_only(self, make_kb: KbFactory) -> None:
        """Both original assertion records must still exist with their original values."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        all_assertions = kb.assertions(subject=entity.id, predicate="Person.name", status=None)
        by_id = {a.id: a for a in all_assertions}
        assert by_id[ada_id].value == "Ada"
        assert by_id[ava_id].value == "Ava"

    def test_no_duplicate_retracted_event_for_an_already_retracted_loser(
        self, make_kb: KbFactory
    ) -> None:
        """A loser that's already `retracted` (e.g. a neutral third party's
        own earlier retract(), KI-033/KI-034) needs no further write -
        re-retracting it is a status no-op and must not record a second,
        resolver-misattributed `retracted` event as if the resolver had
        just done it (found in KI-034's review)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "dave@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)
        kb.retract(ava_id, "dave@example.com")
        events_before = kb.backend.get_assertion_events(ava_id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)

        events_after = kb.backend.get_assertion_events(ava_id)
        assert events_after == events_before
        assert kb.backend.get_assertion(ava_id).status == "retracted"  # type: ignore[union-attr]

    def test_three_member_contradiction_resolves_all_losers(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Eve", "Text", HUMAN_WRITE)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        winner = next(a for a in flagged if a.value == "Ada")
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        kb.resolve_contradiction(contradiction.id, winner.id, REVIEWER)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        retracted = kb.assertions(subject=entity.id, predicate="Person.name", status="retracted")
        assert len(active) == 1
        assert len(retracted) == 2


# ===========================================================================
# Guard rails
# ===========================================================================


class TestResolveContradictionGuards:
    def test_unknown_resolver_raises_auth_error(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        with pytest.raises(AuthError, match="Principal not found"):
            kb.resolve_contradiction(contradiction_id, ada_id, "nobody@example.com")

    def test_non_reviewer_raises_capability_error(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        with pytest.raises(CapabilityError, match="lacks review capability"):
            kb.resolve_contradiction(contradiction_id, ada_id, NON_REVIEWER)

    def test_unknown_contradiction_raises_not_found(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        with pytest.raises(NotFoundError, match="Contradiction not found"):
            kb.resolve_contradiction("nonexistent-id", "some-assertion", REVIEWER)

    def test_winner_not_a_member_raises_validation_error(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, _, _ = _open_contradiction(kb, entity.id)

        with pytest.raises(ValidationError, match="is not a member"):
            kb.resolve_contradiction(contradiction_id, "not-a-real-assertion", REVIEWER)

    def test_already_resolved_contradiction_raises(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)
        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)

        with pytest.raises(ValidationError, match="is not open"):
            kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)

    def test_ai_resolver_raises_capability_error(self, make_kb: KbFactory) -> None:
        """A misconfigured AI principal with review capability must still be
        blocked from resolving contradictions (ADR-0003)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "misconfigured-ai-reviewer",
            kind="ai",
            auth_method="apikey",
            owner=HUMAN_WRITE,
            default_capability="review",
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        with pytest.raises(CapabilityError, match="AI principal and cannot review"):
            kb.resolve_contradiction(contradiction_id, ada_id, "misconfigured-ai-reviewer")

    def test_resolver_cannot_pick_own_authored_assertion_as_winner(
        self, make_kb: KbFactory
    ) -> None:
        """REVIEWER authored one of the two disputed facts - can't
        unilaterally pick it as the winner (KI-026)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        reviewer_authored_id = next(a.id for a in flagged if a.author == REVIEWER)
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        with pytest.raises(CapabilityError, match="cannot resolve a contradiction"):
            kb.resolve_contradiction(contradiction.id, reviewer_authored_id, REVIEWER)

    def test_resolver_cannot_resolve_when_they_authored_a_losing_member(
        self, make_kb: KbFactory
    ) -> None:
        """REVIEWER authored one of the disputed facts, even the one NOT
        picked as winner - still blocked, since resolving still means
        adjudicating a dispute they're a party to (KI-026)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        other_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        with pytest.raises(CapabilityError, match="cannot resolve a contradiction"):
            kb.resolve_contradiction(contradiction.id, other_id, REVIEWER)

    def test_resolver_cannot_resolve_via_delegate_authored_assertion(
        self, make_kb: KbFactory
    ) -> None:
        """A disputed fact asserted by REVIEWER's delegate (acting_as)
        counts the same as one REVIEWER authored directly - the delegation
        chain doesn't launder around the self-resolution guard."""
        kb = _kb(make_kb)
        kb.create_principal(
            "erin@example.com",
            kind="human",
            auth_method="oidc",
            owner=REVIEWER,
            default_capability="write",
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", "erin@example.com", acting_as=REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        delegated_id = next(a.id for a in flagged if a.author == "erin@example.com")
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        with pytest.raises(CapabilityError, match="cannot resolve a contradiction"):
            kb.resolve_contradiction(contradiction.id, delegated_id, REVIEWER)


# ===========================================================================
# retract() contradiction-member guard (KI-033)
# ===========================================================================


class TestRetractContradictionGuard:
    """retract() is a separate path from resolve_contradiction() that can
    reach a similar outcome — a party to a disputed static fact ending the
    dispute in their own favor unilaterally, just by retracting the
    *opposing* flagged member instead of picking a winner (KI-033)."""

    def test_party_cannot_retract_opposing_contradiction_member(self, make_kb: KbFactory) -> None:
        """HUMAN_WRITE authored "Ada", REVIEWER authored "Ava" - HUMAN_WRITE
        can't unilaterally retract REVIEWER's opposing entry."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        opposing_id = next(a.id for a in flagged if a.author == REVIEWER)
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        with pytest.raises(CapabilityError, match="party to"):
            kb.retract(opposing_id, HUMAN_WRITE)

        # Rejected before any write — assertion and contradiction untouched.
        assert kb.backend.get_assertion(opposing_id).status == "flagged"  # type: ignore[union-attr]
        assert kb.backend.get_contradiction(contradiction.id).state == "open"  # type: ignore[union-attr]

    def test_party_cannot_retract_own_contradiction_member(self, make_kb: KbFactory) -> None:
        """Retracting your *own* losing entry ends the dispute in your favor
        just as much as picking it as the winner would - blocked too, same
        as resolve_contradiction's "any member" guard (KI-026)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        own_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)

        with pytest.raises(CapabilityError, match="party to"):
            kb.retract(own_id, HUMAN_WRITE)

    def test_delegate_authored_member_blocks_retraction(self, make_kb: KbFactory) -> None:
        """A disputed fact asserted by REVIEWER's delegate (acting_as) counts
        the same as one REVIEWER authored directly."""
        kb = _kb(make_kb)
        kb.create_principal(
            "erin@example.com",
            kind="human",
            auth_method="oidc",
            owner=REVIEWER,
            default_capability="write",
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", "erin@example.com", acting_as=REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        ada_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)

        with pytest.raises(CapabilityError, match="party to"):
            kb.retract(ada_id, REVIEWER)

    def test_neutral_third_party_can_still_retract_flagged_member(self, make_kb: KbFactory) -> None:
        """A principal with no stake in either disputed value (not an
        author/delegate of any member) is unaffected by this guard."""
        kb = _kb(make_kb)
        kb.create_principal(
            "dave@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        ada_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)

        kb.retract(ada_id, "dave@example.com")

        assert kb.backend.get_assertion(ada_id).status == "retracted"  # type: ignore[union-attr]

    def test_flagged_status_without_open_contradiction_is_unaffected(
        self, make_kb: KbFactory
    ) -> None:
        """Defensive branch: an assertion whose status happens to be
        `flagged` with no matching open contradiction record (there is no
        code path that produces this today - every `flagged` status is
        always paired with a Contradiction row) does not trip this guard;
        retract() proceeds normally rather than treating a `None` lookup as
        a reason to block."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        a = kb.assert_literal(entity.id, "Person.born", "1815", "Text", HUMAN_WRITE)
        kb.backend.set_assertion_status(a.id, "flagged")

        kb.retract(a.id, HUMAN_WRITE)

        assert kb.backend.get_assertion(a.id).status == "retracted"  # type: ignore[union-attr]

    def test_missing_contradiction_member_raises_not_found(self, make_kb: KbFactory) -> None:
        """A contradiction member that can't be found is data corruption
        (assertions are append-only and never deleted, SPEC §5), not a
        benign gap to skip past - matches resolve_contradiction's own
        precedent (KI-026)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "dave@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        _, ada_id, _ = _open_contradiction(kb, entity.id)
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None
        kb.backend.update_contradiction_members(
            contradiction.id, [*contradiction.member_ids, "nonexistent-assertion-id"]
        )

        with pytest.raises(NotFoundError, match="could not be found"):
            kb.retract(ada_id, "dave@example.com")

    def test_party_via_accept_proposal_is_also_blocked(self, make_kb: KbFactory) -> None:
        """The guard also applies when the retraction reaches its effect via
        accept_proposal() (e.g. a lower-capability delegate's retract
        proposal that required review), not just retract()'s own
        auto-accept path - closing the same gap through the review queue,
        not only the direct call."""
        kb = _kb(make_kb)
        kb.create_principal(
            "erin@example.com",
            kind="human",
            auth_method="oidc",
            owner=REVIEWER,
            default_capability="propose",
        )
        kb.create_principal(
            "frank@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        opposing_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)

        # erin's own capability (propose, trust 0) requires review even when
        # delegating through REVIEWER (effective capability is min() of the
        # two, ADR-0003) - so this retract lands in the review queue rather
        # than auto-accepting.
        proposal, decision = kb.retract(opposing_id, "erin@example.com", acting_as=REVIEWER)
        assert proposal.state == "require_review"

        with pytest.raises(CapabilityError, match="party to"):
            kb.accept_proposal(proposal.id, "frank@example.com")

    def test_party_reviewer_cannot_accept_a_neutral_proposers_retract_proposal(
        self, make_kb: KbFactory
    ) -> None:
        """The party doesn't have to be the proposer at all - a reviewer who
        is themselves a party to the contradiction can reach the exact same
        one-sided outcome by *approving* an entirely neutral principal's
        retract proposal, so the guard must check the accepting reviewer
        too, not just the proposal's own author/delegate (found in review)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "grace@example.com", kind="human", auth_method="oidc", default_capability="propose"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        opposing_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)

        # grace has no stake in either disputed value and no delegation -
        # her own low capability (propose, trust 0) is why this lands in
        # the review queue, not any party-to-contradiction reasoning.
        proposal, decision = kb.retract(opposing_id, "grace@example.com")
        assert proposal.state == "require_review"

        # REVIEWER authored the *other* disputed value ("Ava") - approving
        # grace's proposal to retract "Ada" would let REVIEWER settle the
        # dispute in their own favor just by reviewing someone else's
        # proposal instead of retracting directly.
        with pytest.raises(CapabilityError, match="party to"):
            kb.accept_proposal(proposal.id, REVIEWER)

        assert kb.backend.get_assertion(opposing_id).status == "flagged"  # type: ignore[union-attr]
        assert kb.backend.get_proposal(proposal.id).state == "require_review"  # type: ignore[union-attr]


# ===========================================================================
# `retracted` is terminal across contradiction extension (KI-034)
# ===========================================================================


class TestRetractedIsTerminalAcrossExtension:
    """`retracted` is meant to be a terminal status everywhere in the
    codebase (SPEC §5's append-only lifecycle). Extending an already-open
    contradiction with a fresh disputed value previously re-flagged *every*
    existing member unconditionally, including one that had since been
    legitimately retracted (e.g. by a neutral party via retract(), KI-033) -
    resurrecting it back to `flagged`. Only the first test below actually
    exercises the regression (confirmed by reverting the fix locally and
    re-running - it fails without it); the other two pin adjacent, already
    correct-pre-fix invariants (no duplicate event, membership retained for
    audit) that this change deliberately preserves rather than disturbs."""

    def test_extending_open_contradiction_does_not_resurrect_retracted_member(
        self, make_kb: KbFactory
    ) -> None:
        kb = _kb(make_kb)
        kb.create_principal(
            "dave@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)

        # A neutral third party retracts one member outright (allowed -
        # dave is party to neither disputed value, KI-033).
        kb.retract(ada_id, "dave@example.com")
        assert kb.backend.get_assertion(ada_id).status == "retracted"  # type: ignore[union-attr]

        # A third disputed value extends the still-open contradiction.
        kb.propose(entity.id, "Person.name", "Aida", "Text", REVIEWER)

        assert kb.backend.get_assertion(ada_id).status == "retracted"  # type: ignore[union-attr]
        assert kb.backend.get_assertion(ava_id).status == "flagged"  # type: ignore[union-attr]
        flagged_values = {
            a.value
            for a in kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        }
        assert flagged_values == {"Ava", "Aida"}

    def test_extending_open_contradiction_records_no_flagged_event_for_retracted_member(
        self, make_kb: KbFactory
    ) -> None:
        """The retracted member's event trail is untouched - no spurious
        'flagged' event is recorded for it just because the contradiction it
        once belonged to was extended."""
        kb = _kb(make_kb)
        kb.create_principal(
            "dave@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        _, ada_id, _ = _open_contradiction(kb, entity.id)
        kb.retract(ada_id, "dave@example.com")
        events_before = kb.backend.get_assertion_events(ada_id)

        kb.propose(entity.id, "Person.name", "Aida", "Text", REVIEWER)

        events_after = kb.backend.get_assertion_events(ada_id)
        assert events_after == events_before

    def test_retracted_member_stays_in_contradiction_membership_for_audit(
        self, make_kb: KbFactory
    ) -> None:
        """The retracted assertion's id is not dropped from the
        contradiction's own member_ids - only its status stops changing."""
        kb = _kb(make_kb)
        kb.create_principal(
            "dave@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)
        kb.retract(ada_id, "dave@example.com")

        kb.propose(entity.id, "Person.name", "Aida", "Text", REVIEWER)

        contradiction = kb.backend.get_contradiction(contradiction_id)
        assert contradiction is not None
        assert ada_id in contradiction.member_ids

    def test_flag_contradiction_does_not_resurrect_a_retracted_assertion(
        self, make_kb: KbFactory
    ) -> None:
        """flag_contradiction() has its own, separate flagging loop from
        conflict-routing's extend-branch above - found in review to have
        the identical resurrection bug, and it's reachable at only
        `propose` capability (including by an AI principal via MCP's
        ontolith.flag_contradiction, which carries no write capability at
        all)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "dave@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        a = kb.assert_literal(entity.id, "Person.born", "1815", "Text", HUMAN_WRITE)
        b = kb.assert_literal(entity.id, "Person.born", "1816", "Text", HUMAN_WRITE)
        kb.retract(a.id, "dave@example.com")
        assert kb.backend.get_assertion(a.id).status == "retracted"  # type: ignore[union-attr]

        kb.flag_contradiction(a.id, b.id, NON_REVIEWER)

        assert kb.backend.get_assertion(a.id).status == "retracted"  # type: ignore[union-attr]
        assert kb.backend.get_assertion(b.id).status == "flagged"  # type: ignore[union-attr]
