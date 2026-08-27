"""Unit tests for KI-043 / ADR-0030's capability floor as reached through
`resubmit`'s own auto-accept branch specifically.

`conformance/test_contradiction_resolution.py::TestRetractContradictionGuard`
covers the floor itself (write routed to review, review allowed, AI routed
to review, delegation attenuation) through `retract()`'s own auto-accept
path, which is the common case. This file exists only to reach the *other*
call site - `resubmit`'s own review-routing check and, as a race-only
backstop, `_replay_proposal_operations`'s `retract` branch when invoked
from `resubmit`'s auto-accept (not `accept_proposal`, whose reviewer
already satisfies the floor) - a shape `retract()` alone can't reach,
since resubmit requires a prior `request_changes` cycle.
"""

import tempfile
from pathlib import Path
from typing import Any

import pytest

from ontolith import Ontology
from ontolith.core import FixedClock, SequentialIdProvider
from ontolith.core.errors import CapabilityError
from ontolith.govern import AutoAccept, Decision, RequireReview


class _ReviewThenAutoAccept:
    """PolicyStrategy test double: RequireReview on the first evaluation
    (retract()'s own submission), AutoAccept on every subsequent one
    (resubmit) - ThresholdPolicy can never reach resubmit's auto-accept
    branch for the same low-capability author, since its decision depends
    only on immutable principal fields."""

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, proposal: Any, principal: Any, kb: Any, acting_as: Any = None) -> Decision:
        self.calls += 1
        if self.calls == 1:
            return RequireReview(reviewers=[], reason="first evaluation always reviewed")
        return AutoAccept("subsequent evaluation auto-accepted")


def _connect() -> Ontology:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    return Ontology.connect(
        path,
        clock=FixedClock("2025-01-01T00:00:00Z"),
        id_provider=SequentialIdProvider(prefix="t"),
        policy=_ReviewThenAutoAccept(),
    )


def _open_contradiction(kb: Ontology, subject: str, ada_author: str, ava_author: str) -> str:
    """Flags two conflicting static values, using assert_literal (write
    capability, always auto-accepts) so contradiction setup doesn't itself
    depend on the test-double policy under test."""
    kb.assert_literal(subject, "Person.name", "Ada", "Text", author=ada_author)
    kb.assert_literal(subject, "Person.name", "Ava", "Text", author=ava_author)
    flagged = kb.assertions(subject=subject, predicate="Person.name", status="flagged")
    return next(a.id for a in flagged if a.author == ada_author)


class TestResubmitAutoAcceptCapabilityFloor:
    def test_resubmit_routes_to_review_for_write_only_author(self) -> None:
        kb = _connect()
        kb.create_principal("alice", kind="human", default_capability="write")
        kb.create_principal("wendy", kind="human", default_capability="write")
        kb.create_principal("bob", kind="human", default_capability="propose")
        kb.create_principal("carol", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author="alice")
        ada_id = _open_contradiction(kb, entity.id, "alice", "wendy")

        # bob is neutral (party to neither Ada nor Ava) but only
        # propose-capability -> retract() itself requires review.
        proposal, decision = kb.retract(ada_id, "bob")
        assert isinstance(decision, RequireReview)
        kb.request_changes(proposal.id, reviewer="carol", reason="double-check")

        # Policy would auto-accept on this second evaluation, but bob's
        # own capability (still just propose) never rose - KI-043 routes
        # this back to review instead of auto-accepting (or hard-blocking
        # with no path forward, the inversion an earlier version of this
        # fix had).
        proposal, decision = kb.resubmit(proposal.id, author="bob")

        assert isinstance(decision, RequireReview)
        assert kb.backend.get_assertion(ada_id).status == "flagged"  # type: ignore[union-attr]
        reloaded = kb.backend.get_proposal(proposal.id)
        assert reloaded is not None
        assert reloaded.state == "require_review"

        # A review-capable, non-AI, neutral principal can still accept it.
        kb.accept_proposal(proposal.id, "carol")
        assert kb.backend.get_assertion(ada_id).status == "retracted"  # type: ignore[union-attr]
        kb.close()

    def test_resubmit_auto_accept_blocks_a_party_immediately(self) -> None:
        """A party to the contradiction reaching resubmit's own auto-accept
        branch must be blocked immediately (KI-033), not routed to review
        - routing them instead would defer an already-certain rejection
        (the party guard also fires, unconditionally, inside
        accept_proposal's replay) into a permanently-stuck pending
        proposal, exactly the failure mode a second review pass found
        resubmit() was missing relative to retract() itself."""
        kb = _connect()
        kb.create_principal("alice", kind="human", default_capability="write")
        kb.create_principal("wendy", kind="human", default_capability="write")
        kb.create_principal("carol", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author="alice")
        _open_contradiction(kb, entity.id, "alice", "wendy")
        ava_id = next(
            a.id
            for a in kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
            if a.author == "wendy"
        )

        # alice authored "Ada" - a party to the contradiction - trying to
        # retract the opposing "Ava" member. First policy evaluation
        # always requires review (the test-double policy is call-count
        # based, independent of alice's own write capability).
        proposal, decision = kb.retract(ava_id, "alice")
        assert isinstance(decision, RequireReview)
        kb.request_changes(proposal.id, reviewer="carol", reason="double-check")

        # Second evaluation would auto-accept - the party guard must
        # still catch alice immediately here, not route to review.
        with pytest.raises(CapabilityError, match="party to"):
            kb.resubmit(proposal.id, author="alice")

        assert kb.backend.get_assertion(ava_id).status == "flagged"  # type: ignore[union-attr]
        reloaded = kb.backend.get_proposal(proposal.id)
        assert reloaded is not None
        assert reloaded.state == "changes_requested"
        kb.close()

    def test_resubmit_auto_accept_allowed_for_review_capable_author(self) -> None:
        kb = _connect()
        kb.create_principal("alice", kind="human", default_capability="write")
        kb.create_principal("wendy", kind="human", default_capability="write")
        kb.create_principal("dana", kind="human", default_capability="review")
        kb.create_principal("carol", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author="alice")
        ada_id = _open_contradiction(kb, entity.id, "alice", "wendy")

        proposal, decision = kb.retract(ada_id, "dana")
        assert isinstance(decision, RequireReview)
        kb.request_changes(proposal.id, reviewer="carol", reason="double-check")

        proposal, decision = kb.resubmit(proposal.id, author="dana")

        assert isinstance(decision, AutoAccept)
        assert kb.backend.get_assertion(ada_id).status == "retracted"  # type: ignore[union-attr]
        kb.close()


class TestRequireCapabilityToRetractContradictionMemberBackstop:
    """`_require_capability_to_retract_contradiction_member` is, in the non-race
    case, unreachable: `retract()`/`resubmit()`'s own pre-check
    (`_retract_op_review_override`) always routes a below-floor principal
    to review before a transaction that could reach it ever opens. It only
    fires for the narrow race where a contradiction opens concurrently
    between that pre-check and the transaction - impractical to construct
    end-to-end without mocking internals, so this exercises the method
    directly instead of leaving its two raise branches (AI-kind, and
    capability-below-review) untested."""

    def test_raises_for_ai_kind_principal(self) -> None:
        kb = _connect()
        kb.create_principal("alice", kind="human", default_capability="write")
        kb.create_principal("wendy", kind="human", default_capability="write")
        kb.create_principal(
            "frank-ai",
            kind="ai",
            auth_method="workload",
            owner="alice",
            default_capability="review",
        )
        entity = kb.create_entity("Person", author="alice")
        ada_id = _open_contradiction(kb, entity.id, "alice", "wendy")
        frank = kb.backend.get_principal("frank-ai")
        assert frank is not None

        with pytest.raises(CapabilityError, match="AI principal"):
            kb._require_capability_to_retract_contradiction_member(ada_id, frank, None)
        kb.close()

    def test_raises_for_capability_below_review(self) -> None:
        kb = _connect()
        kb.create_principal("alice", kind="human", default_capability="write")
        kb.create_principal("wendy", kind="human", default_capability="write")
        kb.create_principal("dave", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author="alice")
        ada_id = _open_contradiction(kb, entity.id, "alice", "wendy")
        dave = kb.backend.get_principal("dave")
        assert dave is not None

        with pytest.raises(CapabilityError, match="review/admin capability"):
            kb._require_capability_to_retract_contradiction_member(ada_id, dave, None)
        kb.close()

    def test_no_op_for_ordinary_non_contradiction_retraction(self) -> None:
        """A target that isn't a flagged contradiction member is
        unaffected, regardless of the principal's capability."""
        kb = _connect()
        kb.create_principal("alice", kind="human", default_capability="write")
        kb.create_principal("dave", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author="alice")
        assertion = kb.assert_literal(entity.id, "Person.born", "1815", "Text", author="alice")
        dave = kb.backend.get_principal("dave")
        assert dave is not None

        kb._require_capability_to_retract_contradiction_member(assertion.id, dave, None)  # no raise
        kb.close()
