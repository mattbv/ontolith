"""Unit tests for KI-043 / ADR-0030's capability floor as reached through
`resubmit`'s own auto-accept branch specifically.

`conformance/test_contradiction_resolution.py::TestRetractContradictionGuard`
covers the floor itself (write blocked, review allowed, AI blocked,
delegation attenuation) through `retract()`'s own auto-accept path, which
is the common case. This file exists only to reach the *other* call site -
`_replay_proposal_operations`'s `retract` branch when invoked from
`resubmit`'s own auto-accept (not `accept_proposal`, whose reviewer
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
    def test_resubmit_auto_accept_blocked_for_write_only_author(self) -> None:
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

        # Policy auto-accepts on this second evaluation, but bob's own
        # capability (still just propose) never rose - KI-043's floor
        # blocks the retraction anyway.
        with pytest.raises(CapabilityError, match="review/admin capability"):
            kb.resubmit(proposal.id, author="bob")

        assert kb.backend.get_assertion(ada_id).status == "flagged"  # type: ignore[union-attr]
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
