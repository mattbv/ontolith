"""Conformance vectors for SPEC §9 review workflow.

Tests accept_proposal()/reject_proposal()/request_changes() — the human-review
path for proposals that ThresholdPolicy routes to require_review (e.g. AI
principals). All tests use injected clocks and IDs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.core.errors import AuthError, CapabilityError, NotFoundError, ValidationError

T0 = datetime(2025, 1, 1, tzinfo=UTC)

HUMAN_AUTHOR = "alice@example.com"
REVIEWER = "bob@example.com"
AI_AUTHOR = "gpt-agent"
AI_OWNER = HUMAN_AUTHOR


def _kb(make_kb: KbFactory) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider(
        [
            "e-1",
            "a-1",
            "a-2",
            "a-3",
            "a-4",
            "prop-1",
            "prop-2",
            "prop-3",
            "prop-4",
            "contra-1",
        ]
    )
    kb = make_kb(clock, ids)
    kb.create_principal(HUMAN_AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(REVIEWER, kind="human", auth_method="oidc", default_capability="review")
    kb.create_principal(
        AI_AUTHOR, kind="ai", auth_method="apikey", owner=AI_OWNER, default_capability="propose"
    )
    return kb


# ===========================================================================
# Accept path
# ===========================================================================


class TestAcceptProposal:
    """Reviewer accepts a pending proposal → operations applied."""

    def test_accepted_proposal_state(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        assert proposal.state == "require_review"

        accepted = kb.accept_proposal(proposal.id, REVIEWER)
        assert accepted.state == "accepted"

    def test_accepted_proposal_has_decided_at(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        accepted = kb.accept_proposal(proposal.id, REVIEWER)
        assert accepted.decided_at is not None

    def test_assertion_written_after_accept(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        # No assertion yet
        assert kb.assertions(subject=entity.id, predicate="Person.name") == []

        kb.accept_proposal(proposal.id, REVIEWER)

        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].value == "Ada"

    def test_accepted_assertion_carries_proposal_id(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        kb.accept_proposal(proposal.id, REVIEWER)

        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active[0].proposal_id == proposal.id

    def test_accepted_assertion_author_is_original_proposer(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        kb.accept_proposal(proposal.id, REVIEWER)

        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active[0].author == AI_AUTHOR

    def test_accept_retraction_proposal(self, make_kb: KbFactory) -> None:
        """Reviewer can accept an AI-proposed retraction."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        # Human creates an assertion (auto-accepted)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assertion_id = active[0].id

        # AI proposes retraction → require_review
        retract_proposal, _ = kb.retract(assertion_id, AI_AUTHOR)
        assert retract_proposal.state == "require_review"
        assert kb.assertions(subject=entity.id, predicate="Person.name", status="active") != []

        # Reviewer accepts
        kb.accept_proposal(retract_proposal.id, REVIEWER)
        assert kb.assertions(subject=entity.id, predicate="Person.name", status="active") == []
        retracted = kb.assertions(subject=entity.id, predicate="Person.name", status="retracted")
        assert len(retracted) == 1

    def test_accepted_assertion_goes_through_conflict_routing(self, make_kb: KbFactory) -> None:
        """Assertion accepted via review is still subject to SPEC §10 conflict routing."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        # Human writes "Ada" (auto-accepted, active)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)

        # AI proposes "Ava" → require_review
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ava", "Text", AI_AUTHOR, model="test-model-v1"
        )

        # Reviewer accepts → conflict routing triggers, both flagged
        kb.accept_proposal(proposal.id, REVIEWER)

        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert active == []
        assert len(flagged) == 2
        assert {a.value for a in flagged} == {"Ada", "Ava"}

    def test_accept_records_exactly_one_proposal_event(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        kb.accept_proposal(proposal.id, REVIEWER)

        events = kb.backend.get_proposal_events(proposal.id)
        assert len(events) == 1
        assert events[0].type == "accept"
        assert events[0].actor == REVIEWER
        assert events[0].proposal_id == proposal.id

    def test_accept_does_not_overwrite_policy_reason(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        original_policy_reason = proposal.policy_reason
        assert original_policy_reason is not None

        accepted = kb.accept_proposal(proposal.id, REVIEWER)
        assert accepted.policy_reason == original_policy_reason

    def test_multiple_review_cycles_produce_independent_events(self, make_kb: KbFactory) -> None:
        """Accept/reject cycles across different proposals don't overwrite
        each other's events - each proposal_id gets its own event history."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        p1, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="m1")
        p2, _ = kb.propose(entity.id, "Person.born", "1815", "Text", AI_AUTHOR, model="m1")

        kb.accept_proposal(p1.id, REVIEWER)
        kb.reject_proposal(p2.id, REVIEWER, reason="not enough evidence")

        p1_events = kb.backend.get_proposal_events(p1.id)
        p2_events = kb.backend.get_proposal_events(p2.id)
        assert len(p1_events) == 1 and p1_events[0].type == "accept"
        assert len(p2_events) == 1 and p2_events[0].type == "reject"
        assert p2_events[0].detail == "not enough evidence"


# ===========================================================================
# Reject path
# ===========================================================================


class TestRejectProposal:
    """Reviewer rejects a pending proposal → no operations applied."""

    def test_rejected_proposal_state(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        rejected = kb.reject_proposal(proposal.id, REVIEWER)
        assert rejected.state == "rejected"

    def test_rejected_proposal_has_decided_at(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        rejected = kb.reject_proposal(proposal.id, REVIEWER)
        assert rejected.decided_at is not None

    def test_no_assertion_written_after_reject(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        kb.reject_proposal(proposal.id, REVIEWER, reason="Insufficient evidence")
        assert kb.assertions(subject=entity.id, predicate="Person.name") == []

    def test_reject_reason_recorded_as_proposal_event(self, make_kb: KbFactory) -> None:
        """The reviewer's reason is recorded as a structured proposal_event
        (SPEC §9.4), not overwritten into policy_reason - that field remains
        whatever the policy engine set at proposal-creation time."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        original_policy_reason = proposal.policy_reason

        rejected = kb.reject_proposal(proposal.id, REVIEWER, reason="Insufficient evidence")
        assert rejected.policy_reason == original_policy_reason

        events = kb.backend.get_proposal_events(proposal.id)
        assert len(events) == 1
        assert events[0].type == "reject"
        assert events[0].actor == REVIEWER
        assert events[0].detail == "Insufficient evidence"


# ===========================================================================
# Request-changes path
# ===========================================================================


class TestRequestChanges:
    """Reviewer requests changes on a pending proposal — SPEC §9.1's third
    under_review outcome, alongside accept/reject."""

    def test_changes_requested_proposal_state(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        updated = kb.request_changes(proposal.id, REVIEWER)
        assert updated.state == "changes_requested"

    def test_changes_requested_proposal_has_decided_at(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        updated = kb.request_changes(proposal.id, REVIEWER)
        assert updated.decided_at is not None

    def test_no_assertion_written_after_request_changes(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        kb.request_changes(proposal.id, REVIEWER, reason="needs a source")
        assert kb.assertions(subject=entity.id, predicate="Person.name") == []

    def test_reason_recorded_as_proposal_event(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        original_policy_reason = proposal.policy_reason

        updated = kb.request_changes(proposal.id, REVIEWER, reason="needs a source")
        assert updated.policy_reason == original_policy_reason

        events = kb.backend.get_proposal_events(proposal.id)
        assert len(events) == 1
        assert events[0].type == "request_changes"
        assert events[0].actor == REVIEWER
        assert events[0].detail == "needs a source"

    def test_changes_requested_is_a_dead_end_for_further_review(self, make_kb: KbFactory) -> None:
        """changes_requested is not "pending review" — accept/reject/
        request_changes again must all raise, since nothing resubmits a
        changes_requested proposal back to submitted yet (KI-022)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        kb.request_changes(proposal.id, REVIEWER)

        with pytest.raises(ValidationError, match="not pending review"):
            kb.accept_proposal(proposal.id, REVIEWER)
        with pytest.raises(ValidationError, match="not pending review"):
            kb.reject_proposal(proposal.id, REVIEWER)
        with pytest.raises(ValidationError, match="not pending review"):
            kb.request_changes(proposal.id, REVIEWER)


# ===========================================================================
# Guard rails
# ===========================================================================


class TestReviewGuards:
    """Capability checks and state guards."""

    def test_write_only_principal_cannot_accept(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        with pytest.raises(CapabilityError, match="lacks review capability"):
            kb.accept_proposal(proposal.id, HUMAN_AUTHOR)

    def test_write_only_principal_cannot_reject(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        with pytest.raises(CapabilityError, match="lacks review capability"):
            kb.reject_proposal(proposal.id, HUMAN_AUTHOR)

    def test_unknown_reviewer_raises_on_accept(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        with pytest.raises(AuthError, match="Principal not found"):
            kb.accept_proposal(proposal.id, "nobody@example.com")

    def test_unknown_reviewer_raises_on_reject(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        with pytest.raises(AuthError, match="Principal not found"):
            kb.reject_proposal(proposal.id, "nobody@example.com")

    def test_unknown_proposal_raises_on_accept(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        with pytest.raises(NotFoundError, match="Proposal not found"):
            kb.accept_proposal("nonexistent-id", REVIEWER)

    def test_unknown_proposal_raises_on_reject(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        with pytest.raises(NotFoundError, match="Proposal not found"):
            kb.reject_proposal("nonexistent-id", REVIEWER)

    def test_already_accepted_proposal_raises(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        kb.accept_proposal(proposal.id, REVIEWER)

        with pytest.raises(ValidationError, match="not pending review"):
            kb.accept_proposal(proposal.id, REVIEWER)

    def test_already_rejected_proposal_raises(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        kb.reject_proposal(proposal.id, REVIEWER)

        with pytest.raises(ValidationError, match="not pending review"):
            kb.reject_proposal(proposal.id, REVIEWER)

    def test_already_rejected_proposal_cannot_have_changes_requested(
        self, make_kb: KbFactory
    ) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        kb.reject_proposal(proposal.id, REVIEWER)

        with pytest.raises(ValidationError, match="not pending review"):
            kb.request_changes(proposal.id, REVIEWER)

    def test_auto_accepted_proposal_cannot_be_reviewed(self, make_kb: KbFactory) -> None:
        """auto_accepted proposals are already decided — review is not applicable."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        assert proposal.state == "auto_accepted"

        with pytest.raises(ValidationError, match="not pending review"):
            kb.accept_proposal(proposal.id, REVIEWER)

    def test_ai_reviewer_cannot_accept(self, make_kb: KbFactory) -> None:
        """A misconfigured AI principal with review capability must still be
        blocked — ADR-0003's human-in-the-loop guarantee doesn't hold if an
        AI can review (its own or anyone else's) proposals."""
        kb = _kb(make_kb)
        kb.create_principal(
            "misconfigured-ai-reviewer",
            kind="ai",
            auth_method="apikey",
            owner=HUMAN_AUTHOR,
            default_capability="review",
        )
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        with pytest.raises(CapabilityError, match="AI principal and cannot review"):
            kb.accept_proposal(proposal.id, "misconfigured-ai-reviewer")

    def test_ai_reviewer_cannot_reject(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        kb.create_principal(
            "misconfigured-ai-reviewer",
            kind="ai",
            auth_method="apikey",
            owner=HUMAN_AUTHOR,
            default_capability="review",
        )
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        with pytest.raises(CapabilityError, match="AI principal and cannot review"):
            kb.reject_proposal(proposal.id, "misconfigured-ai-reviewer")

    def test_write_only_principal_cannot_request_changes(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        with pytest.raises(CapabilityError, match="lacks review capability"):
            kb.request_changes(proposal.id, HUMAN_AUTHOR)

    def test_unknown_reviewer_raises_on_request_changes(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        with pytest.raises(AuthError, match="Principal not found"):
            kb.request_changes(proposal.id, "nobody@example.com")

    def test_unknown_proposal_raises_on_request_changes(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        with pytest.raises(NotFoundError, match="Proposal not found"):
            kb.request_changes("nonexistent-id", REVIEWER)

    def test_auto_accepted_proposal_cannot_have_changes_requested(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        assert proposal.state == "auto_accepted"

        with pytest.raises(ValidationError, match="not pending review"):
            kb.request_changes(proposal.id, REVIEWER)

    def test_ai_reviewer_cannot_request_changes(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        kb.create_principal(
            "misconfigured-ai-reviewer",
            kind="ai",
            auth_method="apikey",
            owner=HUMAN_AUTHOR,
            default_capability="review",
        )
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        with pytest.raises(CapabilityError, match="AI principal and cannot review"):
            kb.request_changes(proposal.id, "misconfigured-ai-reviewer")

    def test_delegating_principal_cannot_review_its_own_delegated_proposal(
        self, make_kb: KbFactory
    ) -> None:
        """A propose-capability principal delegates (acting_as) to a
        review-capability owner; the delegated proposal still requires
        review (SPEC §8.4: capability is capped at min(), not elevated).
        The owner must not be able to review a proposal made in their own
        name via delegation — that's still self-approval in effect."""
        kb = _kb(make_kb)
        kb.create_principal(
            "carol@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        kb.create_principal(
            "erin@example.com",
            kind="human",
            auth_method="oidc",
            owner="carol@example.com",
            default_capability="propose",
            trust_level=0,
        )
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, decision = kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            "erin@example.com",
            acting_as="carol@example.com",
        )
        assert proposal.state == "require_review"

        with pytest.raises(CapabilityError, match="cannot review their own proposal"):
            kb.accept_proposal(proposal.id, "carol@example.com")
        with pytest.raises(CapabilityError, match="cannot review their own proposal"):
            kb.request_changes(proposal.id, "carol@example.com")


class TestProposalAndContradictionListing:
    """kb.proposals()/kb.contradictions() — the SDK surface reviewers need to
    discover pending work (SPEC §14.1); route_to_review otherwise routes
    into a void with no way to enumerate what it routed."""

    def test_proposals_defaults_to_require_review(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)  # auto_accepted
        pending, _ = kb.propose(
            entity.id, "Person.born", "1815", "Text", AI_AUTHOR, model="test-model-v1"
        )
        assert pending.state == "require_review"

        listed = kb.proposals()
        assert [p.id for p in listed] == [pending.id]

    def test_proposals_state_none_returns_all(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        auto, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        pending, _ = kb.propose(
            entity.id, "Person.born", "1815", "Text", AI_AUTHOR, model="test-model-v1"
        )

        listed = kb.proposals(state=None)
        assert {p.id for p in listed} == {auto.id, pending.id}

    def test_contradictions_defaults_to_open(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        kb.propose(entity.id, "Person.name", "Ava", "Text", HUMAN_AUTHOR)  # -> contradiction

        listed = kb.contradictions()
        assert len(listed) == 1
        assert listed[0].state == "open"

    def test_contradictions_excludes_resolved_by_default(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        kb.propose(entity.id, "Person.name", "Ava", "Text", HUMAN_AUTHOR)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        [contradiction] = kb.contradictions()
        kb.resolve_contradiction(contradiction.id, flagged[0].id, REVIEWER)

        assert kb.contradictions() == []
        assert len(kb.contradictions(state=None)) == 1
        assert kb.contradictions(state="resolved")[0].id == contradiction.id
