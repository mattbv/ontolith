"""Unit tests for governance layer."""

from datetime import UTC, datetime

import pytest

from ontolith.core import Assertion
from ontolith.govern import (
    AutoAccept,
    ConfidenceThreshold,
    Proposal,
    Reject,
    RequireReview,
    RequireReviewByRole,
    SourceQuorum,
    SourceRequired,
    ThresholdPolicy,
)
from ontolith.identity import Principal


class TestProposal:
    """Tests for Proposal model."""

    def test_create_proposal(self) -> None:
        """Proposal can be created."""
        proposal = Proposal(
            id="prop-001",
            namespace="test-ns",
            author="alice@example.com",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            payload={"operations": [{"type": "create_entity"}]},
        )

        assert proposal.id == "prop-001"
        assert proposal.author == "alice@example.com"
        assert proposal.state == "draft"
        assert proposal.decided_at is None

    def test_proposal_immutable(self) -> None:
        """Proposals are immutable."""
        from pydantic import ValidationError

        proposal = Proposal(
            id="prop-001",
            namespace="test",
            author="alice@example.com",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        with pytest.raises(ValidationError):
            proposal.state = "accepted"  # type: ignore


class TestThresholdPolicy:
    """Tests for ThresholdPolicy."""

    def test_human_with_write_auto_accepts(self) -> None:
        """Humans with write capability auto-accept."""
        policy = ThresholdPolicy()

        principal = Principal(
            id="alice@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        proposal = Proposal(
            id="prop-001",
            namespace="test",
            author=principal.id,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        decision = policy.evaluate(proposal, principal)

        assert isinstance(decision, AutoAccept)
        assert "Trusted human" in decision.reason

    def test_ai_requires_review(self) -> None:
        """AI principals always require review."""
        policy = ThresholdPolicy()

        principal = Principal(
            id="bot-001",
            kind="ai",
            owner="alice@example.com",
            auth_method="workload",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        proposal = Proposal(
            id="prop-001",
            namespace="test",
            author=principal.id,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        decision = policy.evaluate(proposal, principal)

        assert isinstance(decision, RequireReview)
        assert principal.owner in decision.reviewers
        assert "AI proposals require review" in decision.reason

    def test_human_with_propose_requires_review(self) -> None:
        """Humans with only propose capability need review."""
        policy = ThresholdPolicy()

        principal = Principal(
            id="bob@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="propose",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        proposal = Proposal(
            id="prop-001",
            namespace="test",
            author=principal.id,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        decision = policy.evaluate(proposal, principal)

        assert isinstance(decision, RequireReview)

    def test_service_with_write_auto_accepts(self) -> None:
        """Service principals with write auto-accept."""
        policy = ThresholdPolicy()

        principal = Principal(
            id="import-service",
            kind="service",
            auth_method="apikey",
            default_capability="write",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        proposal = Proposal(
            id="prop-001",
            namespace="test",
            author=principal.id,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        decision = policy.evaluate(proposal, principal)

        assert isinstance(decision, AutoAccept)
        assert "Trusted service" in decision.reason


class TestThresholdPolicyDelegation:
    """acting_as (SPEC §8.4): effective capability is min(author, acting_as),
    never a wholesale substitution. AI-kind authors always require review
    regardless of delegation (ADR-0003)."""

    def _principal(self, **overrides: object) -> Principal:
        base: dict[str, object] = {
            "id": "p",
            "kind": "human",
            "auth_method": "oidc",
            "default_capability": "propose",
            "trust_level": 0,
            "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        }
        base.update(overrides)
        return Principal(**base)  # type: ignore[arg-type]

    def _proposal(self, author: str = "author") -> Proposal:
        return Proposal(
            id="prop-001",
            namespace="test",
            author=author,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

    def test_ai_author_always_requires_review_regardless_of_delegate_capability(self) -> None:
        """AI author delegating to a write-capable owner still requires review —
        this is the core delegation-bypass fix: AI's own kind must dominate."""
        policy = ThresholdPolicy()
        ai = self._principal(id="bot", kind="ai", owner="owner@example.com")
        owner = self._principal(id="owner@example.com", default_capability="write")

        decision = policy.evaluate(self._proposal("bot"), ai, acting_as=owner)

        assert isinstance(decision, RequireReview)

    def test_propose_delegating_to_write_capped_at_propose(self) -> None:
        """min(propose, write) = propose — the delegate's higher capability does
        not elevate the author (without also meeting the trust>=5 threshold)."""
        policy = ThresholdPolicy()
        author = self._principal(id="author", default_capability="propose", trust_level=0)
        delegate = self._principal(id="delegate", default_capability="write")

        decision = policy.evaluate(self._proposal("author"), author, acting_as=delegate)

        assert isinstance(decision, RequireReview)

    def test_write_delegating_to_propose_capped_at_propose(self) -> None:
        """min(write, propose) = propose — delegating "up" doesn't help either;
        the lower of the two capabilities always governs."""
        policy = ThresholdPolicy()
        author = self._principal(id="author", default_capability="write")
        delegate = self._principal(id="delegate", default_capability="propose", trust_level=0)

        decision = policy.evaluate(self._proposal("author"), author, acting_as=delegate)

        assert isinstance(decision, RequireReview)

    def test_write_delegating_to_write_auto_accepts(self) -> None:
        """min(write, write) = write — both sides sufficiently capable auto-accepts."""
        policy = ThresholdPolicy()
        author = self._principal(id="author", default_capability="write")
        delegate = self._principal(id="delegate", default_capability="write")

        decision = policy.evaluate(self._proposal("author"), author, acting_as=delegate)

        assert isinstance(decision, AutoAccept)

    def test_effective_trust_level_is_the_lower_of_the_two(self) -> None:
        """min(trust_level) governs the propose+trust>=5 auto-accept branch —
        a high-trust delegate cannot lift a low-trust author over the threshold."""
        policy = ThresholdPolicy()
        author = self._principal(id="author", default_capability="propose", trust_level=0)
        delegate = self._principal(id="delegate", default_capability="propose", trust_level=10)

        decision = policy.evaluate(self._proposal("author"), author, acting_as=delegate)

        assert isinstance(decision, RequireReview)

    def test_no_delegation_matches_undelegated_evaluate(self) -> None:
        """acting_as=None behaves identically to the 2-arg call (backward compatible)."""
        policy = ThresholdPolicy()
        author = self._principal(id="author", default_capability="write")

        with_none = policy.evaluate(self._proposal("author"), author, acting_as=None)
        without_arg = policy.evaluate(self._proposal("author"), author)

        assert isinstance(with_none, AutoAccept)
        assert isinstance(without_arg, AutoAccept)


class _FakeKbView:
    """Pure test double for KbView - no backend, just an in-memory list."""

    def __init__(self, assertions: list[Assertion]) -> None:
        self._assertions = assertions

    def assertions(
        self, subject: str | None = None, predicate: str | None = None
    ) -> list[Assertion]:
        return [
            a
            for a in self._assertions
            if (subject is None or a.subject == subject)
            and (predicate is None or a.predicate == predicate)
        ]


class TestSourceQuorum:
    """SourceQuorum (KI-017, ADR-0025): auto-accepts once `threshold` distinct
    sources corroborate the same (subject, predicate, value)."""

    T0 = datetime(2025, 1, 1, tzinfo=UTC)
    # SourceQuorum never reads `principal` - a dummy suffices for every test.
    PRINCIPAL = Principal(id="author", kind="human", auth_method="oidc", created_at=T0)

    def _assertion(
        self,
        subject: str = "e-1",
        predicate: str = "Person.name",
        value: str = "Ada",
        source: str | None = "existing-source",
        value_kind: str = "literal",
    ) -> Assertion:
        return Assertion(
            id="a-existing",
            namespace="test",
            subject=subject,
            predicate=predicate,
            value_kind=value_kind,  # type: ignore[arg-type]
            value_type="Text" if value_kind == "literal" else None,
            value=value,
            author="author",
            source=source,
            asserted_at=self.T0,
        )

    def _proposal(self, operations: list[dict[str, object]]) -> Proposal:
        return Proposal(
            id="prop-001",
            namespace="test",
            author="author",
            created_at=self.T0,
            payload={"operations": operations},
        )

    def _assert_literal_op(
        self,
        subject: str = "e-1",
        predicate: str = "Person.name",
        value: str = "Ada",
        source: str | None = "new-source",
    ) -> dict[str, object]:
        return {
            "kind": "assert_literal",
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "value_type": "Text",
            "source": source,
        }

    def test_constructor_rejects_threshold_below_one(self) -> None:
        with pytest.raises(ValueError, match="threshold"):
            SourceQuorum(threshold=0)

    def test_quorum_reached_auto_accepts(self) -> None:
        """1 existing corroborating source + the proposal's own = threshold 2."""
        policy = SourceQuorum(threshold=2)
        kb = _FakeKbView([self._assertion(source="source-a")])

        decision = policy.evaluate(
            self._proposal([self._assert_literal_op(source="source-b")]), self.PRINCIPAL, kb
        )

        assert isinstance(decision, AutoAccept)
        assert "2/2" in decision.reason

    def test_quorum_not_met_requires_review(self) -> None:
        """Only the proposal's own source (no existing corroboration) - below threshold 2."""
        policy = SourceQuorum(threshold=2, reviewers=["reviewer@example.com"])
        kb = _FakeKbView([])

        decision = policy.evaluate(
            self._proposal([self._assert_literal_op(source="source-b")]), self.PRINCIPAL, kb
        )

        assert isinstance(decision, RequireReview)
        assert decision.reviewers == ["reviewer@example.com"]
        assert "1/2" in decision.reason

    def test_retraction_always_requires_review(self) -> None:
        """Retractions never auto-accept, even at threshold=1."""
        policy = SourceQuorum(threshold=1)
        kb = _FakeKbView([])

        decision = policy.evaluate(
            self._proposal([{"kind": "retract", "assertion_id": "a-1"}]), self.PRINCIPAL, kb
        )

        assert isinstance(decision, RequireReview)
        assert "retract" in decision.reason.lower()

    def test_sourceless_proposal_requires_review(self) -> None:
        """A proposal with no source can't establish a quorum, regardless of corroboration."""
        policy = SourceQuorum(threshold=1)
        kb = _FakeKbView([self._assertion(source="source-a")])

        decision = policy.evaluate(
            self._proposal([self._assert_literal_op(source=None)]), self.PRINCIPAL, kb
        )

        assert isinstance(decision, RequireReview)
        assert "no source" in decision.reason.lower()

    def test_mismatched_value_does_not_corroborate(self) -> None:
        """An existing assertion with a different value doesn't count toward quorum."""
        policy = SourceQuorum(threshold=2)
        kb = _FakeKbView([self._assertion(value="Ada", source="source-a")])

        decision = policy.evaluate(
            self._proposal([self._assert_literal_op(value="Adaeze", source="source-b")]),
            self.PRINCIPAL,
            kb,
        )

        assert isinstance(decision, RequireReview)
        assert "1/2" in decision.reason

    def test_existing_assertion_without_source_does_not_corroborate(self) -> None:
        """An existing assertion with source=None can't establish corroboration."""
        policy = SourceQuorum(threshold=2)
        kb = _FakeKbView([self._assertion(source=None)])

        decision = policy.evaluate(
            self._proposal([self._assert_literal_op(source="source-b")]), self.PRINCIPAL, kb
        )

        assert isinstance(decision, RequireReview)
        assert "1/2" in decision.reason

    def test_assert_ref_uses_target_not_value(self) -> None:
        """assert_ref operations are keyed on `target`, not `value` (which they lack)."""
        policy = SourceQuorum(threshold=2)
        kb = _FakeKbView(
            [
                self._assertion(
                    predicate="Person.employer",
                    value="org-1",
                    source="source-a",
                    value_kind="ref",
                )
            ]
        )
        op = {
            "kind": "assert_ref",
            "subject": "e-1",
            "predicate": "Person.employer",
            "target": "org-1",
            "source": "source-b",
        }

        decision = policy.evaluate(self._proposal([op]), self.PRINCIPAL, kb)

        assert isinstance(decision, AutoAccept)

    def test_read_capability_principal_rejected(self) -> None:
        """Read-capability principals are rejected outright, before any
        source-counting - mirrors ThresholdPolicy's own read-only rejection,
        since propose()/propose_ref()/retract() have no capability pre-check
        of their own (KI-015 update, ADR-0025)."""
        policy = SourceQuorum(threshold=1)
        reader = Principal(
            id="reader",
            kind="human",
            auth_method="oidc",
            default_capability="read",
            created_at=self.T0,
        )
        kb = _FakeKbView([self._assertion(source="source-a")])

        decision = policy.evaluate(
            self._proposal([self._assert_literal_op(source="source-b")]), reader, kb
        )

        assert isinstance(decision, Reject)

    def test_delegated_read_capability_capped_and_rejected(self) -> None:
        """min(capability) applies to SourceQuorum's own floor too, same as
        ThresholdPolicy: delegating to a write-capability principal doesn't
        lift a read-capability author above the floor."""
        policy = SourceQuorum(threshold=1)
        reader = Principal(
            id="reader",
            kind="human",
            auth_method="oidc",
            default_capability="read",
            created_at=self.T0,
        )
        delegate = Principal(
            id="delegate",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            created_at=self.T0,
        )
        kb = _FakeKbView([])

        decision = policy.evaluate(
            self._proposal([self._assert_literal_op(source="source-a")]),
            reader,
            kb,
            acting_as=delegate,
        )

        assert isinstance(decision, Reject)

    def test_empty_operations_requires_review(self) -> None:
        """An empty operations list requires review rather than raising
        IndexError - defensive handling for a payload shape no current
        caller produces but that Proposal's typing (dict[str, Any]) permits."""
        policy = SourceQuorum(threshold=1)
        kb = _FakeKbView([])

        decision = policy.evaluate(self._proposal([]), self.PRINCIPAL, kb)

        assert isinstance(decision, RequireReview)

    def test_unknown_operation_kind_requires_review(self) -> None:
        """An unrecognized op kind requires review rather than raising
        KeyError on a missing `value`/`target` - defensive handling, since
        Proposal.payload isn't a closed, validated shape at this layer."""
        policy = SourceQuorum(threshold=1)
        kb = _FakeKbView([])
        op = {"kind": "create_entity", "subject": "e-1"}

        decision = policy.evaluate(self._proposal([op]), self.PRINCIPAL, kb)

        assert isinstance(decision, RequireReview)


class TestConfidenceThreshold:
    """ConfidenceThreshold (KI-069, SPEC §9.2): auto-accepts once a proposal's
    own asserted confidence meets `threshold`."""

    T0 = datetime(2025, 1, 1, tzinfo=UTC)
    # Never reads `kb` - a dummy suffices for every test.
    PRINCIPAL = Principal(id="author", kind="human", auth_method="oidc", created_at=T0)

    def _proposal(self, operations: list[dict[str, object]]) -> Proposal:
        return Proposal(
            id="prop-001",
            namespace="test",
            author="author",
            created_at=self.T0,
            payload={"operations": operations},
        )

    def _op(self, confidence: float | None, kind: str = "assert_literal") -> dict[str, object]:
        return {
            "kind": kind,
            "subject": "e-1",
            "predicate": "Person.name",
            "value": "Ada",
            "value_type": "Text",
            "confidence": confidence,
        }

    def test_constructor_rejects_threshold_outside_unit_interval(self) -> None:
        with pytest.raises(ValueError, match="threshold"):
            ConfidenceThreshold(threshold=1.5)
        with pytest.raises(ValueError, match="threshold"):
            ConfidenceThreshold(threshold=-0.1)

    def test_boundary_thresholds_are_valid(self) -> None:
        """0.0 and 1.0 are both valid thresholds (inclusive range)."""
        ConfidenceThreshold(threshold=0.0)
        ConfidenceThreshold(threshold=1.0)

    def test_confidence_at_threshold_auto_accepts(self) -> None:
        """Threshold comparison is inclusive (>=), not strict (>)."""
        policy = ConfidenceThreshold(threshold=0.8)

        decision = policy.evaluate(self._proposal([self._op(0.8)]), self.PRINCIPAL)

        assert isinstance(decision, AutoAccept)
        assert "0.8" in decision.reason

    def test_confidence_above_threshold_auto_accepts(self) -> None:
        policy = ConfidenceThreshold(threshold=0.5)

        decision = policy.evaluate(self._proposal([self._op(0.9)]), self.PRINCIPAL)

        assert isinstance(decision, AutoAccept)

    def test_confidence_below_threshold_requires_review(self) -> None:
        policy = ConfidenceThreshold(threshold=0.8, reviewers=["reviewer@example.com"])

        decision = policy.evaluate(self._proposal([self._op(0.7)]), self.PRINCIPAL)

        assert isinstance(decision, RequireReview)
        assert decision.reviewers == ["reviewer@example.com"]
        assert "0.7" in decision.reason and "0.8" in decision.reason

    def test_missing_confidence_requires_review(self) -> None:
        """A proposal with no confidence never auto-accepts, regardless of
        threshold - not treated as confidence 0 or confidence 1."""
        policy = ConfidenceThreshold(threshold=0.0)

        decision = policy.evaluate(self._proposal([self._op(None)]), self.PRINCIPAL)

        assert isinstance(decision, RequireReview)
        assert "no confidence" in decision.reason.lower()

    def test_retraction_always_requires_review(self) -> None:
        policy = ConfidenceThreshold(threshold=0.0)

        decision = policy.evaluate(
            self._proposal([{"kind": "retract", "assertion_id": "a-1"}]), self.PRINCIPAL
        )

        assert isinstance(decision, RequireReview)
        assert "retract" in decision.reason.lower()

    def test_assert_ref_confidence_is_evaluated(self) -> None:
        """assert_ref operations carry confidence the same way assert_literal does."""
        policy = ConfidenceThreshold(threshold=0.5)
        op = self._op(0.9, kind="assert_ref")

        decision = policy.evaluate(self._proposal([op]), self.PRINCIPAL)

        assert isinstance(decision, AutoAccept)

    def test_empty_operations_requires_review(self) -> None:
        policy = ConfidenceThreshold(threshold=0.0)

        decision = policy.evaluate(self._proposal([]), self.PRINCIPAL)

        assert isinstance(decision, RequireReview)

    def test_unknown_operation_kind_requires_review(self) -> None:
        policy = ConfidenceThreshold(threshold=0.0)
        op = {"kind": "create_entity", "subject": "e-1"}

        decision = policy.evaluate(self._proposal([op]), self.PRINCIPAL)

        assert isinstance(decision, RequireReview)

    def test_read_capability_principal_rejected(self) -> None:
        """Enforces the KI-015 capability floor itself, mirroring SourceQuorum."""
        policy = ConfidenceThreshold(threshold=0.0)
        reader = Principal(
            id="reader",
            kind="human",
            auth_method="oidc",
            default_capability="read",
            created_at=self.T0,
        )

        decision = policy.evaluate(self._proposal([self._op(1.0)]), reader)

        assert isinstance(decision, Reject)

    def test_delegated_read_capability_capped_and_rejected(self) -> None:
        """min(capability) applies here too - delegating to a write-capability
        principal doesn't lift a read-capability author above the floor."""
        policy = ConfidenceThreshold(threshold=0.0)
        reader = Principal(
            id="reader",
            kind="human",
            auth_method="oidc",
            default_capability="read",
            created_at=self.T0,
        )
        delegate = Principal(
            id="delegate",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            created_at=self.T0,
        )

        decision = policy.evaluate(self._proposal([self._op(1.0)]), reader, acting_as=delegate)

        assert isinstance(decision, Reject)


class TestSourceRequired:
    """SourceRequired (KI-069, SPEC §9.2): auto-accepts only when the
    proposal's operation carries a non-empty `source`."""

    T0 = datetime(2025, 1, 1, tzinfo=UTC)
    PRINCIPAL = Principal(id="author", kind="human", auth_method="oidc", created_at=T0)

    def _proposal(self, operations: list[dict[str, object]]) -> Proposal:
        return Proposal(
            id="prop-001",
            namespace="test",
            author="author",
            created_at=self.T0,
            payload={"operations": operations},
        )

    def _op(self, source: str | None, kind: str = "assert_literal") -> dict[str, object]:
        return {
            "kind": kind,
            "subject": "e-1",
            "predicate": "Person.name",
            "value": "Ada",
            "value_type": "Text",
            "source": source,
        }

    def test_source_present_auto_accepts(self) -> None:
        policy = SourceRequired()

        decision = policy.evaluate(self._proposal([self._op("some-source")]), self.PRINCIPAL)

        assert isinstance(decision, AutoAccept)
        assert "some-source" in decision.reason

    def test_source_missing_requires_review(self) -> None:
        policy = SourceRequired(reviewers=["reviewer@example.com"])

        decision = policy.evaluate(self._proposal([self._op(None)]), self.PRINCIPAL)

        assert isinstance(decision, RequireReview)
        assert decision.reviewers == ["reviewer@example.com"]
        assert "no source" in decision.reason.lower()

    def test_empty_string_source_requires_review(self) -> None:
        """An empty string source is treated the same as no source at all."""
        policy = SourceRequired()

        decision = policy.evaluate(self._proposal([self._op("")]), self.PRINCIPAL)

        assert isinstance(decision, RequireReview)

    def test_retraction_always_requires_review(self) -> None:
        policy = SourceRequired()

        decision = policy.evaluate(
            self._proposal([{"kind": "retract", "assertion_id": "a-1"}]), self.PRINCIPAL
        )

        assert isinstance(decision, RequireReview)
        assert "retract" in decision.reason.lower()

    def test_assert_ref_source_is_evaluated(self) -> None:
        policy = SourceRequired()
        op = self._op("some-source", kind="assert_ref")

        decision = policy.evaluate(self._proposal([op]), self.PRINCIPAL)

        assert isinstance(decision, AutoAccept)

    def test_empty_operations_requires_review(self) -> None:
        policy = SourceRequired()

        decision = policy.evaluate(self._proposal([]), self.PRINCIPAL)

        assert isinstance(decision, RequireReview)

    def test_unknown_operation_kind_requires_review(self) -> None:
        policy = SourceRequired()
        op = {"kind": "create_entity", "subject": "e-1"}

        decision = policy.evaluate(self._proposal([op]), self.PRINCIPAL)

        assert isinstance(decision, RequireReview)

    def test_read_capability_principal_rejected(self) -> None:
        policy = SourceRequired()
        reader = Principal(
            id="reader",
            kind="human",
            auth_method="oidc",
            default_capability="read",
            created_at=self.T0,
        )

        decision = policy.evaluate(self._proposal([self._op("some-source")]), reader)

        assert isinstance(decision, Reject)

    def test_delegated_read_capability_capped_and_rejected(self) -> None:
        policy = SourceRequired()
        reader = Principal(
            id="reader",
            kind="human",
            auth_method="oidc",
            default_capability="read",
            created_at=self.T0,
        )
        delegate = Principal(
            id="delegate",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            created_at=self.T0,
        )

        decision = policy.evaluate(
            self._proposal([self._op("some-source")]), reader, acting_as=delegate
        )

        assert isinstance(decision, Reject)


class TestRequireReviewByRole:
    """RequireReviewByRole (KI-069, SPEC §9.2): always routes to review,
    assigning reviewers by `principal.metadata["role"]`."""

    T0 = datetime(2025, 1, 1, tzinfo=UTC)
    # `proposal` isn't read at all by this strategy - a minimal stand-in suffices.
    PROPOSAL = Proposal(id="prop-001", namespace="test", author="author", created_at=T0, payload={})

    def _principal(self, role: str | None = None, capability: str = "propose") -> Principal:
        return Principal(
            id="author",
            kind="human",
            auth_method="oidc",
            default_capability=capability,
            created_at=self.T0,
            metadata={"role": role} if role is not None else {},
        )

    def test_mapped_role_gets_its_configured_reviewers(self) -> None:
        policy = RequireReviewByRole(
            {"legal": ["legal-reviewer@example.com"], "eng": ["tech-lead@example.com"]}
        )

        decision = policy.evaluate(self.PROPOSAL, self._principal(role="legal"))

        assert isinstance(decision, RequireReview)
        assert decision.reviewers == ["legal-reviewer@example.com"]
        assert "legal" in decision.reason

    def test_different_mapped_role_gets_its_own_reviewers(self) -> None:
        """Proves the role lookup is keyed correctly, not just returning the
        first configured entry regardless of which role was declared."""
        policy = RequireReviewByRole(
            {"legal": ["legal-reviewer@example.com"], "eng": ["tech-lead@example.com"]}
        )

        decision = policy.evaluate(self.PROPOSAL, self._principal(role="eng"))

        assert isinstance(decision, RequireReview)
        assert decision.reviewers == ["tech-lead@example.com"]

    def test_unmapped_role_falls_back_to_default(self) -> None:
        policy = RequireReviewByRole(
            {"legal": ["legal-reviewer@example.com"]}, default=["fallback@example.com"]
        )

        decision = policy.evaluate(self.PROPOSAL, self._principal(role="marketing"))

        assert isinstance(decision, RequireReview)
        assert decision.reviewers == ["fallback@example.com"]
        assert "no reviewers configured" in decision.reason.lower()

    def test_no_declared_role_falls_back_to_default(self) -> None:
        policy = RequireReviewByRole(
            {"legal": ["legal-reviewer@example.com"]}, default=["fallback@example.com"]
        )

        decision = policy.evaluate(self.PROPOSAL, self._principal(role=None))

        assert isinstance(decision, RequireReview)
        assert decision.reviewers == ["fallback@example.com"]
        assert "no declared role" in decision.reason.lower()

    def test_no_declared_role_and_no_default_is_empty_not_an_error(self) -> None:
        policy = RequireReviewByRole({"legal": ["legal-reviewer@example.com"]})

        decision = policy.evaluate(self.PROPOSAL, self._principal(role=None))

        assert isinstance(decision, RequireReview)
        assert decision.reviewers == []

    def test_never_auto_accepts(self) -> None:
        """Even a mapped role with configured reviewers still requires
        review - this strategy's whole purpose is routing, not approving."""
        policy = RequireReviewByRole({"legal": ["legal-reviewer@example.com"]})

        decision = policy.evaluate(self.PROPOSAL, self._principal(role="legal"))

        assert isinstance(decision, RequireReview)

    def test_role_read_from_principal_not_acting_as(self) -> None:
        """Delegation doesn't change which reviewers get assigned - role
        attaches to the real author, not whoever they're acting as (mirrors
        ThresholdPolicy's AI-kind-never-laundered-via-delegation precedent)."""
        policy = RequireReviewByRole(
            {"legal": ["legal-reviewer@example.com"], "eng": ["tech-lead@example.com"]}
        )
        author = self._principal(role="eng")
        delegate = Principal(
            id="delegate",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            created_at=self.T0,
            metadata={"role": "legal"},
        )

        decision = policy.evaluate(self.PROPOSAL, author, acting_as=delegate)

        assert isinstance(decision, RequireReview)
        assert decision.reviewers == ["tech-lead@example.com"]

    def test_read_capability_principal_rejected(self) -> None:
        policy = RequireReviewByRole({"legal": ["legal-reviewer@example.com"]})

        decision = policy.evaluate(self.PROPOSAL, self._principal(role="legal", capability="read"))

        assert isinstance(decision, Reject)

    def test_delegated_read_capability_capped_and_rejected(self) -> None:
        policy = RequireReviewByRole({"legal": ["legal-reviewer@example.com"]})
        reader = self._principal(role="legal", capability="read")
        delegate = Principal(
            id="delegate",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            created_at=self.T0,
        )

        decision = policy.evaluate(self.PROPOSAL, reader, acting_as=delegate)

        assert isinstance(decision, Reject)
