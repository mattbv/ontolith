"""Unit tests for Validator invocation (KI-041, KI-042, ADR-0029).

Two invocation points, each backed by its own Ontology constructor
parameter:
- `validators` (per-assertion, synchronous, blocking) at every point an
  assertion actually commits: assert_literal, assert_ref, propose/
  propose_ref's auto-accept path, and accept_proposal/resubmit's replay.
- `completeness_validators` (whole-entity), only from accept_proposal,
  after all of an accepted proposal's operations have landed.
"""

import tempfile
from pathlib import Path
from typing import Any

import pytest

from ontolith import Ontology
from ontolith.core import Assertion, FixedClock, SequentialIdProvider
from ontolith.core.errors import ValidationError
from ontolith.govern import AutoAccept, Decision, RequireReview
from ontolith.govern.proposal import Proposal
from ontolith.plugins.reference.required_fields_validator import RequiredFieldsValidator

REJECT_VALUE = "REJECT-ME"


class _RejectMarkerValue:
    """Test double Validator: rejects any assertion whose value is the marker."""

    def __init__(self, message: str = "marker value rejected") -> None:
        self.message = message
        self.seen: list[str] = []

    def validate(self, assertion: Assertion, kb: Any) -> list[str]:
        self.seen.append(assertion.id)
        if assertion.value == REJECT_VALUE:
            return [self.message]
        return []


class _ReviewThenAutoAccept:
    """Test double PolicyStrategy: RequireReview on the first evaluation
    (submission), AutoAccept on every subsequent one (resubmission) - used
    to reach resubmit's own auto-accept branch, which ThresholdPolicy can
    never reach for the same proposal (its decision depends only on
    immutable principal fields, so re-evaluating never changes the answer).
    """

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, proposal: Any, principal: Any, kb: Any, acting_as: Any = None) -> Decision:
        self.calls += 1
        if self.calls == 1:
            return RequireReview(reviewers=[], reason="first evaluation always reviewed")
        return AutoAccept("subsequent evaluation auto-accepted")


class _AlwaysPasses:
    """Test double Validator that never objects, used to confirm multiple
    validators are all consulted and their messages aggregated."""

    def __init__(self, message: str | None = None) -> None:
        self.message = message
        self.called = False

    def validate(self, assertion: Assertion, kb: Any) -> list[str]:
        self.called = True
        return [self.message] if self.message else []


def _temp_db() -> Path:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        return Path(f.name)


def _connect(**kwargs: Any) -> Ontology:
    return Ontology.connect(
        _temp_db(),
        clock=FixedClock("2025-01-01T00:00:00Z"),
        id_provider=SequentialIdProvider(prefix="t"),
        **kwargs,
    )


class TestPerAssertionValidatorsDirectWrites:
    def test_assert_literal_rejected_and_not_persisted(self) -> None:
        validator = _RejectMarkerValue()
        kb = _connect(validators=[validator])
        kb.create_principal("alice", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author="alice")

        with pytest.raises(ValidationError, match="marker value rejected"):
            kb.assert_literal(entity.id, "Person.name", REJECT_VALUE, "Text", author="alice")

        assert kb.assertions(subject=entity.id) == []
        kb.close()

    def test_assert_literal_allowed_when_not_flagged(self) -> None:
        validator = _RejectMarkerValue()
        kb = _connect(validators=[validator])
        kb.create_principal("alice", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author="alice")

        kb.assert_literal(entity.id, "Person.name", "Ada Lovelace", "Text", author="alice")

        assert len(kb.assertions(subject=entity.id)) == 1
        assert validator.seen  # was consulted
        kb.close()

    def test_assert_ref_rejected_and_not_persisted(self) -> None:
        validator = _RejectMarkerValue()
        kb = _connect(validators=[validator])
        kb.create_principal("alice", kind="human", default_capability="write")
        person = kb.create_entity("Person", author="alice")

        with pytest.raises(ValidationError, match="marker value rejected"):
            kb.assert_ref(person.id, "Person.employer", REJECT_VALUE, author="alice")

        assert kb.assertions(subject=person.id) == []
        kb.close()

    def test_multiple_validators_aggregate_messages(self) -> None:
        first = _AlwaysPasses("first objection")
        second = _AlwaysPasses("second objection")
        kb = _connect(validators=[first, second])
        kb.create_principal("alice", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author="alice")

        with pytest.raises(ValidationError) as exc_info:
            kb.assert_literal(entity.id, "Person.name", "Ada Lovelace", "Text", author="alice")

        assert "first objection" in str(exc_info.value)
        assert "second objection" in str(exc_info.value)
        assert first.called
        assert second.called
        kb.close()


class TestPerAssertionValidatorsProposeAutoAccept:
    def test_propose_auto_accept_rejected_leaves_nothing_persisted(self) -> None:
        validator = _RejectMarkerValue()
        kb = _connect(validators=[validator])
        # write capability -> ThresholdPolicy auto-accepts a human's propose
        kb.create_principal("alice", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author="alice")

        with pytest.raises(ValidationError, match="marker value rejected"):
            kb.propose(entity.id, "Person.name", REJECT_VALUE, "Text", author="alice")

        assert kb.assertions(subject=entity.id) == []
        kb.close()

    def test_propose_ref_auto_accept_rejected_leaves_nothing_persisted(self) -> None:
        validator = _RejectMarkerValue()
        kb = _connect(validators=[validator])
        kb.create_principal("alice", kind="human", default_capability="write")
        person = kb.create_entity("Person", author="alice")

        with pytest.raises(ValidationError, match="marker value rejected"):
            kb.propose_ref(person.id, "Person.employer", REJECT_VALUE, author="alice")

        assert kb.assertions(subject=person.id) == []
        kb.close()

    def test_propose_auto_accept_allowed(self) -> None:
        validator = _RejectMarkerValue()
        kb = _connect(validators=[validator])
        kb.create_principal("alice", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author="alice")

        kb.propose(entity.id, "Person.name", "Ada Lovelace", "Text", author="alice")

        assert len(kb.assertions(subject=entity.id)) == 1
        kb.close()


class TestPerAssertionValidatorsAcceptProposalReplay:
    def test_accept_proposal_replay_runs_validators_and_rolls_back(self) -> None:
        validator = _RejectMarkerValue()
        kb = _connect(validators=[validator])
        # propose capability + trust_level 0 -> ThresholdPolicy requires review
        kb.create_principal("bob", kind="human", default_capability="propose")
        kb.create_principal("carol", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author="bob")

        proposal, decision = kb.propose(
            entity.id, "Person.name", REJECT_VALUE, "Text", author="bob"
        )
        assert decision.__class__.__name__ == "RequireReview"

        with pytest.raises(ValidationError, match="marker value rejected"):
            kb.accept_proposal(proposal.id, reviewer="carol")

        # Rolled back: proposal still pending, no assertion landed.
        reloaded = kb.backend.get_proposal(proposal.id)
        assert reloaded is not None
        assert reloaded.state == "require_review"
        assert kb.assertions(subject=entity.id) == []
        kb.close()

    def test_accept_proposal_replay_allowed(self) -> None:
        validator = _RejectMarkerValue()
        kb = _connect(validators=[validator])
        kb.create_principal("bob", kind="human", default_capability="propose")
        kb.create_principal("carol", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author="bob")

        proposal, _decision = kb.propose(
            entity.id, "Person.name", "Ada Lovelace", "Text", author="bob"
        )
        kb.accept_proposal(proposal.id, reviewer="carol")

        assert len(kb.assertions(subject=entity.id)) == 1
        kb.close()

    def test_accept_proposal_replay_runs_validators_on_assert_ref_ops_too(self) -> None:
        validator = _RejectMarkerValue()
        kb = _connect(validators=[validator])
        kb.create_principal("bob", kind="human", default_capability="propose")
        kb.create_principal("carol", kind="human", default_capability="review")
        person = kb.create_entity("Person", author="bob")

        proposal, decision = kb.propose_ref(
            person.id, "Person.employer", REJECT_VALUE, author="bob"
        )
        assert isinstance(decision, RequireReview)

        with pytest.raises(ValidationError, match="marker value rejected"):
            kb.accept_proposal(proposal.id, reviewer="carol")

        assert kb.assertions(subject=person.id) == []
        kb.close()


class TestEmptyValidatorLists:
    def test_no_validators_configured_is_a_no_op(self) -> None:
        kb = _connect()  # validators/completeness_validators both default to None
        kb.create_principal("alice", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author="alice")

        kb.assert_literal(entity.id, "Person.name", "Ada Lovelace", "Text", author="alice")

        assert len(kb.assertions(subject=entity.id)) == 1
        kb.close()

    def test_explicit_empty_lists_are_a_no_op(self) -> None:
        kb = _connect(validators=[], completeness_validators=[])
        kb.create_principal("bob", kind="human", default_capability="propose")
        kb.create_principal("carol", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author="bob")

        proposal, _decision = kb.propose(
            entity.id, "Person.name", "Ada Lovelace", "Text", author="bob"
        )
        kb.accept_proposal(proposal.id, reviewer="carol")

        assert len(kb.assertions(subject=entity.id)) == 1
        kb.close()


class TestCompletenessValidatorsAcceptProposalOnly:
    def test_accept_proposal_rejects_incomplete_entity(self) -> None:
        rfv = RequiredFieldsValidator({"Person": ("name", "email")})
        kb = _connect(completeness_validators=[rfv])
        kb.create_principal("bob", kind="human", default_capability="propose")
        kb.create_principal("carol", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author="bob")

        proposal, _decision = kb.propose(
            entity.id, "Person.name", "Ada Lovelace", "Text", author="bob"
        )
        with pytest.raises(ValidationError, match="missing required predicate 'email'"):
            kb.accept_proposal(proposal.id, reviewer="carol")

        reloaded = kb.backend.get_proposal(proposal.id)
        assert reloaded is not None
        assert reloaded.state == "require_review"
        kb.close()

    def test_accept_proposal_accepts_once_entity_becomes_complete(self) -> None:
        rfv = RequiredFieldsValidator({"Person": ("name", "email")})
        kb = _connect(completeness_validators=[rfv])
        kb.create_principal("bob", kind="human", default_capability="propose")
        kb.create_principal("carol", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author="bob")

        name_proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada Lovelace", "Text", author="bob"
        )
        with pytest.raises(ValidationError):
            kb.accept_proposal(name_proposal.id, reviewer="carol")

        # Direct write (bypasses completeness_validators entirely) fills the gap.
        kb.create_principal("alice", kind="human", default_capability="write")
        kb.assert_literal(entity.id, "Person.email", "ada@example.com", "Text", author="alice")

        # Now the same proposal's completeness check sees name+email both present.
        kb.accept_proposal(name_proposal.id, reviewer="carol")
        accepted = kb.backend.get_proposal(name_proposal.id)
        assert accepted is not None
        assert accepted.state == "accepted"
        kb.close()

    def test_direct_writes_bypass_completeness_validators(self) -> None:
        rfv = RequiredFieldsValidator({"Person": ("name", "email")})
        kb = _connect(completeness_validators=[rfv])
        kb.create_principal("alice", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author="alice")

        # A direct write leaving the entity incomplete is NOT rejected -
        # completeness_validators only run from accept_proposal (ADR-0029).
        kb.assert_literal(entity.id, "Person.name", "Ada Lovelace", "Text", author="alice")

        assert len(kb.assertions(subject=entity.id)) == 1
        kb.close()

    def test_propose_auto_accept_bypasses_completeness_validators(self) -> None:
        rfv = RequiredFieldsValidator({"Person": ("name", "email")})
        kb = _connect(completeness_validators=[rfv])
        kb.create_principal("alice", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author="alice")

        # write capability -> auto-accept; completeness_validators don't run here.
        kb.propose(entity.id, "Person.name", "Ada Lovelace", "Text", author="alice")

        assert len(kb.assertions(subject=entity.id)) == 1
        kb.close()

    def test_retract_via_accept_proposal_re_triggers_completeness_failure(self) -> None:
        """A retract op is the one thing that can *reduce* completeness -
        accept_proposal's completeness check must see it too, not just
        assert_literal/assert_ref ops (found in review)."""
        rfv = RequiredFieldsValidator({"Person": ("name",)})
        kb = _connect(completeness_validators=[rfv])
        kb.create_principal("alice", kind="human", default_capability="write")
        kb.create_principal("bob", kind="human", default_capability="propose")
        kb.create_principal("carol", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author="alice", natural_key="ada")
        # Direct write bypasses completeness_validators (by design) - entity
        # starts out complete.
        kb.assert_literal(entity.id, "Person.name", "Ada Lovelace", "Text", author="alice")
        [name_assertion] = kb.assertions(subject=entity.id, predicate="Person.name")

        proposal, decision = kb.retract(name_assertion.id, author="bob")
        assert isinstance(decision, RequireReview)

        with pytest.raises(ValidationError, match="missing required predicate 'name'"):
            kb.accept_proposal(proposal.id, reviewer="carol")

        # Rolled back: the retraction never took effect.
        assert kb.assertions(subject=entity.id, predicate="Person.name") == [name_assertion]
        kb.close()

    def test_completeness_validator_checks_each_subject_independently(self) -> None:
        """A proposal touching two different subjects must not let one
        entity's completeness (or incompleteness) leak into the other's
        check."""
        rfv = RequiredFieldsValidator({"Person": ("name",)})
        kb = _connect(completeness_validators=[rfv])
        kb.create_principal("alice", kind="human", default_capability="write")
        kb.create_principal("bob", kind="human", default_capability="propose")
        kb.create_principal("carol", kind="human", default_capability="review")
        already_complete = kb.create_entity("Person", author="alice", natural_key="alpha")
        kb.assert_literal(
            already_complete.id, "Person.name", "Alpha Person", "Text", author="alice"
        )
        still_incomplete = kb.create_entity("Person", author="alice", natural_key="beta")

        now = kb.clock.now()

        def _email_op(subject: str) -> dict[str, object]:
            return {
                "kind": "assert_literal",
                "subject": subject,
                "predicate": "Person.email",
                "value": "person@example.com",
                "value_type": "Text",
                "temporality": "static",
                "confidence": None,
                "source": None,
                "rationale": None,
                "model": None,
                "valid_from": None,
                "valid_to": None,
            }

        # propose()/propose_ref() only ever create single-operation
        # proposals - a multi-subject proposal is hand-built here to reach
        # this shape at all.
        proposal = Proposal(
            id=kb.id_provider.next(),
            namespace=kb.namespace,
            author="bob",
            state="require_review",
            created_at=now,
            payload={
                "operations": [
                    _email_op(already_complete.id),
                    _email_op(still_incomplete.id),
                ]
            },
        )
        kb.backend.put_proposal(proposal)

        with pytest.raises(ValidationError) as exc_info:
            kb.accept_proposal(proposal.id, reviewer="carol")

        assert "'beta' missing required predicate 'name'" in str(exc_info.value)
        assert "'alpha'" not in str(exc_info.value)
        kb.close()


class TestResubmitAutoAccept:
    def test_resubmit_auto_accept_runs_validators_but_not_completeness_validators(self) -> None:
        seen = _RejectMarkerValue()  # always allows; records what it saw
        # Always objects - if resubmit's auto-accept branch ran this, the
        # assertion below the resubmit call would never be reached.
        always_objects = _AlwaysPasses("completeness validator should not run here")
        kb = _connect(
            policy=_ReviewThenAutoAccept(),
            validators=[seen],
            completeness_validators=[always_objects],
        )
        kb.create_principal("bob", kind="human", default_capability="propose")
        kb.create_principal("carol", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author="bob")

        proposal, decision = kb.propose(
            entity.id, "Person.name", "Ada Lovelace", "Text", author="bob"
        )
        assert isinstance(decision, RequireReview)
        kb.request_changes(proposal.id, reviewer="carol", reason="please double-check")

        proposal, decision = kb.resubmit(proposal.id, author="bob")

        assert isinstance(decision, AutoAccept)
        assert len(kb.assertions(subject=entity.id)) == 1
        assert seen.seen  # the per-assertion validators list WAS consulted
        assert not always_objects.called  # the completeness list was NOT
        kb.close()

    def test_resubmit_auto_accept_validator_rejection_rolls_back(self) -> None:
        kb = _connect(policy=_ReviewThenAutoAccept(), validators=[_RejectMarkerValue()])
        kb.create_principal("bob", kind="human", default_capability="propose")
        kb.create_principal("carol", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author="bob")

        proposal, decision = kb.propose(
            entity.id, "Person.name", REJECT_VALUE, "Text", author="bob"
        )
        assert isinstance(decision, RequireReview)
        kb.request_changes(proposal.id, reviewer="carol", reason="please double-check")

        with pytest.raises(ValidationError, match="marker value rejected"):
            kb.resubmit(proposal.id, author="bob")

        assert kb.assertions(subject=entity.id) == []
        # Rolled back: the "auto_accepted" state write inside the same
        # transaction is undone too, not just the assertion.
        reloaded = kb.backend.get_proposal(proposal.id)
        assert reloaded is not None
        assert reloaded.state == "changes_requested"
        kb.close()
