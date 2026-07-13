"""Conformance vectors for trust levels and delegation (SPEC §8, ADR-0003).

Trust levels: non-AI principals with 'propose' capability + trust_level >= 5
are auto-accepted without requiring write capability. AI principals always
require review regardless of trust level (ADR-0003).

Delegation (acting_as): a principal can act on behalf of another (typically
an AI agent acting as its owner). Per SPEC §8.4, the effective capability is
min(capability(author), capability(acting_as)) and effective trust_level is
the more conservative (lower) of the two — never a wholesale substitution of
the delegating principal's capability. AI-kind authors always require review
regardless of delegation (ADR-0003): the author's own kind is never
laundered away by delegating to a higher-capability owner. Authorization
rule: acting_as must equal the author's owner field (or author itself).
Borrowing a third party's privileges is rejected with CapabilityError.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.core.errors import AuthError, CapabilityError
from ontolith.govern import AutoAccept, RequireReview

T0 = datetime(2025, 1, 1, tzinfo=UTC)

HUMAN_WRITE = "alice@example.com"
HUMAN_PROPOSE_LOW = "bob@example.com"  # propose + trust_level=3 → require_review
HUMAN_PROPOSE_HIGH = "carol@example.com"  # propose + trust_level=5 → auto_accept
HUMAN_WRITE2 = "dave@example.com"  # write, owner=alice — for non-AI delegation vectors
HUMAN_PROPOSE_OWNED = "erin@example.com"  # propose/trust=0, owner=alice — min() capping vector
AI_AGENT = "scout-agent"  # owner=alice (HUMAN_WRITE)
AI_AGENT2 = "research-bot"  # owner=carol (HUMAN_PROPOSE_HIGH)
AI_AGENT3 = "analyst-bot"  # owner=bob (HUMAN_PROPOSE_LOW)


def _kb(make_kb: KbFactory) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(80)])
    kb = make_kb(clock, ids)
    kb.create_principal(HUMAN_WRITE, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(
        HUMAN_PROPOSE_LOW,
        kind="human",
        auth_method="oidc",
        default_capability="propose",
        trust_level=3,
    )
    kb.create_principal(
        HUMAN_PROPOSE_HIGH,
        kind="human",
        auth_method="oidc",
        default_capability="propose",
        trust_level=5,
    )
    kb.create_principal(
        AI_AGENT,
        kind="ai",
        auth_method="apikey",
        owner=HUMAN_WRITE,
        default_capability="propose",
        trust_level=0,
    )
    kb.create_principal(
        AI_AGENT2,
        kind="ai",
        auth_method="apikey",
        owner=HUMAN_PROPOSE_HIGH,
        default_capability="propose",
        trust_level=0,
    )
    kb.create_principal(
        AI_AGENT3,
        kind="ai",
        auth_method="apikey",
        owner=HUMAN_PROPOSE_LOW,
        default_capability="propose",
        trust_level=0,
    )
    kb.create_principal(
        HUMAN_WRITE2,
        kind="human",
        auth_method="oidc",
        owner=HUMAN_WRITE,
        default_capability="write",
    )
    kb.create_principal(
        HUMAN_PROPOSE_OWNED,
        kind="human",
        auth_method="oidc",
        owner=HUMAN_WRITE,
        default_capability="propose",
        trust_level=0,
    )
    return kb


# ===========================================================================
# Trust levels in ThresholdPolicy
# ===========================================================================


class TestTrustLevels:
    """trust_level >= 5 with 'propose' capability auto-accepts (no write needed)."""

    def test_propose_low_trust_requires_review(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(entity.id, "Person.name", "Bob", "Text", HUMAN_PROPOSE_LOW)
        assert isinstance(decision, RequireReview)
        assert proposal.state == "require_review"

    def test_propose_high_trust_auto_accepted(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(
            entity.id, "Person.name", "Carol", "Text", HUMAN_PROPOSE_HIGH
        )
        assert isinstance(decision, AutoAccept)
        assert proposal.state == "auto_accepted"

    def test_trust_threshold_is_5(self, make_kb: KbFactory) -> None:
        """trust_level=4 still requires review; trust_level=5 auto-accepts."""
        from ontolith.govern.policy import AutoAccept, RequireReview, ThresholdPolicy
        from ontolith.govern.proposal import Proposal
        from ontolith.identity import Principal

        policy = ThresholdPolicy()
        base = {
            "id": "p",
            "kind": "human",
            "auth_method": "oidc",
            "default_capability": "propose",
            "created_at": T0,
        }
        prop = Proposal(id="x", namespace="default", author="p", created_at=T0, payload={})

        p4 = Principal(**{**base, "trust_level": 4})  # type: ignore[arg-type]
        p5 = Principal(**{**base, "trust_level": 5})  # type: ignore[arg-type]

        assert isinstance(policy.evaluate(prop, p4), RequireReview)
        assert isinstance(policy.evaluate(prop, p5), AutoAccept)

    def test_high_trust_assertion_written_immediately(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Carol", "Text", HUMAN_PROPOSE_HIGH)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].value == "Carol"

    def test_high_trust_policy_reason_mentions_trust(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, _ = kb.propose(entity.id, "Person.name", "Carol", "Text", HUMAN_PROPOSE_HIGH)
        assert proposal.policy_reason is not None
        assert "trust" in proposal.policy_reason.lower()

    def test_high_trust_ai_direct_propose_still_requires_review(self, make_kb: KbFactory) -> None:
        """AI principals always require review — trust level does not override (ADR-0003)."""
        from ontolith.govern.policy import RequireReview, ThresholdPolicy
        from ontolith.govern.proposal import Proposal
        from ontolith.identity import Principal

        policy = ThresholdPolicy()
        prop = Proposal(id="x", namespace="default", author="bot", created_at=T0, payload={})
        ai_high_trust = Principal(
            id="bot",
            kind="ai",
            auth_method="apikey",
            owner="human@example.com",
            default_capability="propose",
            trust_level=10,
            created_at=T0,
        )
        assert isinstance(policy.evaluate(prop, ai_high_trust), RequireReview)


# ===========================================================================
# Delegation: acting_as authorization
# ===========================================================================


class TestDelegationAuthorization:
    """Authorization gate: only owner → agent delegation is permitted."""

    def test_unauthorized_delegation_raises_capability_error(self, make_kb: KbFactory) -> None:
        """Agent acting as a third party (not its owner) is rejected."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        with pytest.raises(CapabilityError, match="not authorized to act as"):
            kb.propose(
                entity.id,
                "Person.name",
                "Ada",
                "Text",
                AI_AGENT,  # owner=alice
                acting_as=HUMAN_PROPOSE_HIGH,  # carol — not alice
                model="test-model-v1",
            )

    def test_unauthorized_delegation_on_retract_raises(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        with pytest.raises(CapabilityError, match="not authorized to act as"):
            kb.retract(active[0].id, AI_AGENT, acting_as=HUMAN_PROPOSE_HIGH)

    def test_unknown_delegating_principal_raises_auth_error(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        with pytest.raises(AuthError, match="Delegating principal not found"):
            kb.propose(
                entity.id,
                "Person.name",
                "Ada",
                "Text",
                AI_AGENT,
                acting_as="nobody@example.com",
                model="test-model-v1",
            )

    def test_self_delegation_is_permitted(self, make_kb: KbFactory) -> None:
        """acting_as == author is a no-op and should not raise."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, _ = kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            HUMAN_WRITE,
            acting_as=HUMAN_WRITE,
        )
        assert proposal.acting_as == HUMAN_WRITE


# ===========================================================================
# Delegation: acting_as wiring in propose()
# ===========================================================================


class TestDelegationPropose:
    """Delegation uses min(capability(author), capability(acting_as)) (SPEC §8.4);
    AI-kind authors always require review regardless of delegation (ADR-0003)."""

    def test_ai_acting_as_owner_write_still_requires_review(self, make_kb: KbFactory) -> None:
        """AI (owner=alice/write) acting as alice → still RequireReview.

        The AI's own kind dominates and is never laundered away by delegating
        to a write-capable owner (ADR-0003) — this was the CRITICAL governance
        bypass: an AI could previously auto-accept by naming a trusted owner.
        """
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            AI_AGENT,
            acting_as=HUMAN_WRITE,
            model="test-model-v1",
        )
        assert isinstance(decision, RequireReview)
        assert proposal.state == "require_review"

    def test_ai_acting_as_owner_low_trust_requires_review(self, make_kb: KbFactory) -> None:
        """AI (owner=bob/propose/trust=3) acting as bob → RequireReview."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            AI_AGENT3,
            acting_as=HUMAN_PROPOSE_LOW,
            model="test-model-v1",
        )
        assert isinstance(decision, RequireReview)
        assert proposal.state == "require_review"

    def test_ai_acting_as_owner_high_trust_still_requires_review(self, make_kb: KbFactory) -> None:
        """AI (owner=carol/propose/trust=5) acting as carol → still RequireReview.

        Trust elevation belongs to the delegate, not the AI author — ADR-0003's
        "AI proposals always require review" is checked before any
        capability/trust math, so it can't be bypassed via a trusted owner.
        """
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            AI_AGENT2,
            acting_as=HUMAN_PROPOSE_HIGH,
            model="test-model-v1",
        )
        assert isinstance(decision, RequireReview)
        assert proposal.state == "require_review"

    def test_low_capability_delegating_to_write_capped_by_min(self, make_kb: KbFactory) -> None:
        """SPEC §8.4: effective capability is min(author, acting_as), not acting_as
        alone. A propose-capability principal delegating to a write-capable owner
        does NOT inherit write — it's capped at propose (and still needs trust>=5
        to auto-accept, which this principal lacks)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            HUMAN_PROPOSE_OWNED,
            acting_as=HUMAN_WRITE,
        )
        assert isinstance(decision, RequireReview)
        assert proposal.state == "require_review"

    def test_write_delegating_to_write_auto_accepted(self, make_kb: KbFactory) -> None:
        """Both author and delegate have write capability: min(write, write) = write
        → AutoAccept. Positive-path coverage for non-AI delegation."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            HUMAN_WRITE2,
            acting_as=HUMAN_WRITE,
        )
        assert isinstance(decision, AutoAccept)
        assert proposal.state == "auto_accepted"

    def test_delegation_stamped_on_proposal(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, _ = kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            AI_AGENT,
            acting_as=HUMAN_WRITE,
            model="test-model-v1",
        )
        assert proposal.acting_as == HUMAN_WRITE
        assert proposal.author == AI_AGENT

    def test_delegation_stamped_on_assertion(self, make_kb: KbFactory) -> None:
        """Non-AI delegation (auto-accepted) stamps author/acting_as on the assertion."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE2, acting_as=HUMAN_WRITE)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].author == HUMAN_WRITE2
        assert active[0].acting_as == HUMAN_WRITE

    def test_no_acting_as_behaviour_unchanged(self, make_kb: KbFactory) -> None:
        """Without acting_as, AI still requires review (no delegation privilege)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AGENT, model="test-model-v1"
        )
        assert isinstance(decision, RequireReview)
        assert proposal.acting_as is None

    def test_delegation_survives_the_review_accept_path(self, make_kb: KbFactory) -> None:
        """AI proposals always require review (ADR-0003), so review-accept is
        the primary path delegated AI assertions actually take. acting_as
        must survive replay in accept_proposal, not just the auto-accept
        path already covered by test_delegation_stamped_on_assertion —
        regression for the bug where accept_proposal silently dropped it."""
        kb = _kb(make_kb)
        kb.create_principal(
            "reviewer@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            AI_AGENT,
            acting_as=HUMAN_WRITE,
            model="test-model-v1",
        )
        assert isinstance(decision, RequireReview)

        kb.accept_proposal(proposal.id, "reviewer@example.com")

        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].author == AI_AGENT
        assert active[0].acting_as == HUMAN_WRITE


# ===========================================================================
# Delegation: acting_as wiring in retract()
# ===========================================================================


class TestDelegationRetract:
    """Delegation on retract() mirrors propose() delegation semantics."""

    def test_ai_retract_acting_as_owner_still_requires_review(self, make_kb: KbFactory) -> None:
        """AI-initiated retraction via delegation still requires review (ADR-0003)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        proposal, decision = kb.retract(active[0].id, AI_AGENT, acting_as=HUMAN_WRITE)
        assert isinstance(decision, RequireReview)
        assert proposal.state == "require_review"
        assert proposal.acting_as == HUMAN_WRITE

    def test_ai_delegated_retraction_pending_assertion_still_active(
        self, make_kb: KbFactory
    ) -> None:
        """AI-initiated retraction stays pending — the assertion is not removed
        until a reviewer accepts it."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        kb.retract(active[0].id, AI_AGENT, acting_as=HUMAN_WRITE)
        still_active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(still_active) == 1

    def test_write_delegation_retraction_auto_accepted_removes_assertion(
        self, make_kb: KbFactory
    ) -> None:
        """Non-AI delegation: min(write, write) = write → AutoAccept, retraction applied."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        proposal, decision = kb.retract(active[0].id, HUMAN_WRITE2, acting_as=HUMAN_WRITE)
        assert isinstance(decision, AutoAccept)
        assert kb.assertions(subject=entity.id, predicate="Person.name", status="active") == []

    def test_unknown_delegating_principal_on_retract_raises(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        with pytest.raises(AuthError, match="Delegating principal not found"):
            kb.retract(active[0].id, AI_AGENT, acting_as="ghost@example.com")
