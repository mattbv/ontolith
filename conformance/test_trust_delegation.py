"""Conformance vectors for trust levels and delegation (SPEC §8, ADR-0003).

Trust levels: principals with 'propose' capability + trust_level >= 5 are
auto-accepted without requiring write capability.

Delegation (acting_as): a principal (typically an AI agent) can act on behalf
of another principal. Policy is evaluated using the delegating principal's
capability and trust level; both author and acting_as are recorded in provenance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.core.errors import AuthError
from ontolith.govern import AutoAccept, RequireReview

T0 = datetime(2025, 1, 1, tzinfo=UTC)

HUMAN_WRITE = "alice@example.com"
HUMAN_PROPOSE_LOW = "bob@example.com"  # propose + trust_level=3 → require_review
HUMAN_PROPOSE_HIGH = "carol@example.com"  # propose + trust_level=5 → auto_accept
AI_AGENT = "scout-agent"
AI_OWNER = HUMAN_WRITE


def _kb(tmp_path: Path) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(60)])
    kb = Ontology.connect(tmp_path / "test.db", clock=clock, id_provider=ids)
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
        owner=AI_OWNER,
        default_capability="propose",
        trust_level=0,
    )
    return kb


# ===========================================================================
# Trust levels in ThresholdPolicy
# ===========================================================================


class TestTrustLevels:
    """trust_level >= 5 with 'propose' capability auto-accepts (no write needed)."""

    def test_propose_low_trust_requires_review(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(entity.id, "Person.name", "Bob", "Text", HUMAN_PROPOSE_LOW)
        assert isinstance(decision, RequireReview)
        assert proposal.state == "require_review"

    def test_propose_high_trust_auto_accepted(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(
            entity.id, "Person.name", "Carol", "Text", HUMAN_PROPOSE_HIGH
        )
        assert isinstance(decision, AutoAccept)
        assert proposal.state == "auto_accepted"

    def test_trust_threshold_is_5(self, tmp_path: Path) -> None:
        """trust_level=4 still requires review; trust_level=5 auto-accepts."""
        from ontolith.govern.policy import AutoAccept, RequireReview, ThresholdPolicy
        from ontolith.identity import Principal

        policy = ThresholdPolicy()
        base = {
            "id": "p",
            "kind": "human",
            "auth_method": "oidc",
            "default_capability": "propose",
            "created_at": T0,
        }
        from ontolith.govern.proposal import Proposal

        prop = Proposal(
            id="x",
            namespace="default",
            author="p",
            created_at=T0,
            payload={},
        )

        p4 = Principal(**{**base, "trust_level": 4})  # type: ignore[arg-type]
        p5 = Principal(**{**base, "trust_level": 5})  # type: ignore[arg-type]

        assert isinstance(policy.evaluate(prop, p4), RequireReview)
        assert isinstance(policy.evaluate(prop, p5), AutoAccept)

    def test_high_trust_assertion_written_immediately(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Carol", "Text", HUMAN_PROPOSE_HIGH)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].value == "Carol"

    def test_high_trust_policy_reason_mentions_trust(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(
            entity.id, "Person.name", "Carol", "Text", HUMAN_PROPOSE_HIGH
        )
        assert proposal.policy_reason is not None
        assert "trust" in proposal.policy_reason.lower()


# ===========================================================================
# Delegation: acting_as wiring in propose()
# ===========================================================================


class TestDelegationPropose:
    """AI agent acts on behalf of a write-capable human — proposal is auto-accepted."""

    def test_ai_acting_as_human_write_auto_accepted(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            AI_AGENT,
            acting_as=HUMAN_WRITE,
        )
        assert isinstance(decision, AutoAccept)
        assert proposal.state == "auto_accepted"

    def test_delegation_stamped_on_proposal(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, _ = kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            AI_AGENT,
            acting_as=HUMAN_WRITE,
        )
        assert proposal.acting_as == HUMAN_WRITE
        assert proposal.author == AI_AGENT

    def test_delegation_stamped_on_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            AI_AGENT,
            acting_as=HUMAN_WRITE,
        )
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].author == AI_AGENT
        assert active[0].acting_as == HUMAN_WRITE

    def test_ai_acting_as_low_trust_human_requires_review(self, tmp_path: Path) -> None:
        """Delegation uses delegating principal's trust — low trust still queues."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            AI_AGENT,
            acting_as=HUMAN_PROPOSE_LOW,
        )
        assert isinstance(decision, RequireReview)
        assert proposal.state == "require_review"

    def test_ai_acting_as_high_trust_human_auto_accepted(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            AI_AGENT,
            acting_as=HUMAN_PROPOSE_HIGH,
        )
        assert isinstance(decision, AutoAccept)
        assert proposal.state == "auto_accepted"

    def test_unknown_delegating_principal_raises_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        with pytest.raises(AuthError, match="Delegating principal not found"):
            kb.propose(
                entity.id,
                "Person.name",
                "Ada",
                "Text",
                AI_AGENT,
                acting_as="nobody@example.com",
            )

    def test_no_acting_as_behaviour_unchanged(self, tmp_path: Path) -> None:
        """Without acting_as, AI still requires review."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        proposal, decision = kb.propose(entity.id, "Person.name", "Ada", "Text", AI_AGENT)
        assert isinstance(decision, RequireReview)
        assert proposal.acting_as is None


# ===========================================================================
# Delegation: acting_as wiring in retract()
# ===========================================================================


class TestDelegationRetract:
    """Delegation on retract() mirrors propose() delegation semantics."""

    def test_ai_retract_acting_as_human_write_auto_accepted(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        proposal, decision = kb.retract(active[0].id, AI_AGENT, acting_as=HUMAN_WRITE)
        assert isinstance(decision, AutoAccept)
        assert proposal.state == "auto_accepted"
        assert proposal.acting_as == HUMAN_WRITE

    def test_delegation_retraction_removes_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        kb.retract(active[0].id, AI_AGENT, acting_as=HUMAN_WRITE)
        assert kb.assertions(subject=entity.id, predicate="Person.name", status="active") == []

    def test_unknown_delegating_principal_on_retract_raises(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        with pytest.raises(AuthError, match="Delegating principal not found"):
            kb.retract(active[0].id, AI_AGENT, acting_as="ghost@example.com")
