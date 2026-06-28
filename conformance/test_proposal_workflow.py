"""Conformance vectors for SPEC §9 proposal workflow.

Tests the propose() and retract() paths through ThresholdPolicy, covering
auto-accept (human/write) and require-review (AI) scenarios.
All tests use injected clocks and IDs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.core.errors import StorageError
from ontolith.govern import AutoAccept, RequireReview

T0 = datetime(2025, 1, 1, tzinfo=UTC)
HUMAN_AUTHOR = "alice@example.com"
AI_AUTHOR = "gpt-agent"
AI_OWNER = "alice@example.com"


def _kb_human(tmp_path: Path) -> Ontology:
    """KB with a human/write principal."""
    clock = FixedClock(T0)
    ids = FixedIdProvider(
        [
            "p-human",
            "e-1",
            "a-1",
            "a-2",
            "a-3",
            "prop-1",
            "prop-2",
            "prop-3",
            "prop-4",
            "contra-1",
        ]
    )
    kb = Ontology.connect(tmp_path / "test.db", clock=clock, id_provider=ids)
    kb.create_principal(HUMAN_AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    return kb


def _kb_ai(tmp_path: Path) -> Ontology:
    """KB with an AI principal (owner = HUMAN_AUTHOR)."""
    clock = FixedClock(T0)
    ids = FixedIdProvider(
        [
            "p-human",
            "p-ai",
            "e-1",
            "a-1",
            "prop-1",
            "prop-2",
        ]
    )
    kb = Ontology.connect(tmp_path / "test.db", clock=clock, id_provider=ids)
    kb.create_principal(HUMAN_AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(
        AI_AUTHOR, kind="ai", auth_method="apikey", owner=AI_OWNER, default_capability="propose"
    )
    return kb


# ===========================================================================
# Auto-accept path — human/write principal
# ===========================================================================


class TestAutoAccept:
    """SPEC §9 auto-accept path for trusted human principals."""

    def test_proposal_state_is_auto_accepted(self, tmp_path: Path) -> None:
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, decision = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        assert proposal.state == "auto_accepted"

    def test_decision_is_auto_accept_instance(self, tmp_path: Path) -> None:
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        _, decision = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        assert isinstance(decision, AutoAccept)

    def test_assertion_is_written_to_storage(self, tmp_path: Path) -> None:
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].value == "Ada"

    def test_proposal_is_stored_in_backend(self, tmp_path: Path) -> None:
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        stored = kb.backend.get_proposal(proposal.id)
        assert stored is not None
        assert stored.state == "auto_accepted"
        assert stored.id == proposal.id

    def test_assertion_carries_proposal_id(self, tmp_path: Path) -> None:
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].proposal_id == proposal.id

    def test_payload_contains_operation(self, tmp_path: Path) -> None:
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        ops = proposal.payload.get("operations", [])
        assert len(ops) == 1
        op = ops[0]
        assert op["kind"] == "assert_literal"
        assert op["subject"] == entity.id
        assert op["predicate"] == "Person.name"
        assert op["value"] == "Ada"
        assert op["value_type"] == "Text"

    def test_decided_at_is_set_on_auto_accepted(self, tmp_path: Path) -> None:
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        assert proposal.decided_at is not None

    def test_policy_reason_is_set(self, tmp_path: Path) -> None:
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        assert proposal.policy_reason is not None
        assert len(proposal.policy_reason) > 0

    def test_unknown_author_raises_storage_error(self, tmp_path: Path) -> None:
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        with pytest.raises(StorageError):
            kb.propose(entity.id, "Person.name", "Ada", "Text", "nobody@example.com")


# ===========================================================================
# Require-review path — AI principal
# ===========================================================================


class TestRequireReview:
    """SPEC §9 require-review path for AI principals."""

    def test_proposal_state_is_require_review(self, tmp_path: Path) -> None:
        kb = _kb_ai(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, decision = kb.propose(entity.id, "Person.name", "Ada", "Text", AI_AUTHOR)
        assert proposal.state == "require_review"

    def test_decision_is_require_review_instance(self, tmp_path: Path) -> None:
        kb = _kb_ai(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        _, decision = kb.propose(entity.id, "Person.name", "Ada", "Text", AI_AUTHOR)
        assert isinstance(decision, RequireReview)

    def test_no_assertion_written_for_require_review(self, tmp_path: Path) -> None:
        kb = _kb_ai(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AI_AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active == []

    def test_proposal_stored_with_require_review_state(self, tmp_path: Path) -> None:
        kb = _kb_ai(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI_AUTHOR)
        stored = kb.backend.get_proposal(proposal.id)
        assert stored is not None
        assert stored.state == "require_review"

    def test_ai_reviewers_include_owner(self, tmp_path: Path) -> None:
        kb = _kb_ai(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        _, decision = kb.propose(entity.id, "Person.name", "Ada", "Text", AI_AUTHOR)
        assert isinstance(decision, RequireReview)
        assert AI_OWNER in decision.reviewers

    def test_decided_at_not_set_for_require_review(self, tmp_path: Path) -> None:
        kb = _kb_ai(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI_AUTHOR)
        assert proposal.decided_at is None


# ===========================================================================
# Retraction workflow
# ===========================================================================


class TestRetract:
    """SPEC §9 retract() — governed retraction via proposal path."""

    def test_retract_auto_accepted_for_human_write(self, tmp_path: Path) -> None:
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assertion_id = active[0].id

        proposal, decision = kb.retract(assertion_id, HUMAN_AUTHOR)
        assert isinstance(decision, AutoAccept)
        assert proposal.state == "auto_accepted"

    def test_retracted_assertion_excluded_from_default_query(self, tmp_path: Path) -> None:
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assertion_id = active[0].id

        kb.retract(assertion_id, HUMAN_AUTHOR)

        still_active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert still_active == []

    def test_retracted_assertion_retained_in_storage(self, tmp_path: Path) -> None:
        """Append-only invariant: retracted assertions must still exist."""
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assertion_id = active[0].id

        kb.retract(assertion_id, HUMAN_AUTHOR)

        retracted = kb.assertions(subject=entity.id, predicate="Person.name", status="retracted")
        assert len(retracted) == 1
        assert retracted[0].id == assertion_id
        assert retracted[0].value == "Ada"

    def test_retract_unknown_principal_raises_storage_error(self, tmp_path: Path) -> None:
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        with pytest.raises(StorageError):
            kb.retract(active[0].id, "nobody@example.com")

    def test_retract_payload_contains_operation(self, tmp_path: Path) -> None:
        kb = _kb_human(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assertion_id = active[0].id

        proposal, _ = kb.retract(assertion_id, HUMAN_AUTHOR)
        ops = proposal.payload.get("operations", [])
        assert len(ops) == 1
        assert ops[0]["kind"] == "retract"
        assert ops[0]["assertion_id"] == assertion_id
