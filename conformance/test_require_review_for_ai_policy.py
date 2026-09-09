"""Conformance vector: RequireReviewForAI policy strategy (SPEC §9.2, KI-088).

Deeper edge-case coverage beyond the SPEC-level contract pinned here (KI-015
capability floor, AI-kind never laundered via `acting_as`, determinism/
statelessness) lives alongside every other strategy's own conformance file
in this directory — this file follows the exact same shape as
`test_require_review_by_role_policy.py`, this strategy's closest sibling
(both always-a-fixed-outcome-for-a-given-input strategies with no `kb`
read), plus one end-to-end test proving the `Composite(all=[
RequireReviewForAI(), SourceQuorum(...)])` pattern the class's own
docstring documents actually works as described.
"""

from __future__ import annotations

from datetime import UTC, datetime

from conformance.conftest import KbFactory
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.govern.policy import (
    AutoAccept,
    Composite,
    Reject,
    RequireReview,
    RequireReviewForAI,
    SourceQuorum,
)
from ontolith.govern.proposal import Proposal
from ontolith.identity import Principal

T0 = datetime(2025, 1, 1, tzinfo=UTC)

POLICY = RequireReviewForAI()

PROPOSAL = Proposal(
    id="prop-001",
    namespace="default",
    author="author",
    created_at=T0,
    payload={},
)


def _principal(
    kind: str, owner: str | None = None, default_capability: str = "propose"
) -> Principal:
    return Principal(
        id="p",
        kind=kind,
        auth_method="oidc" if kind != "ai" else "apikey",
        default_capability=default_capability,
        owner=owner,
        created_at=T0,
    )


def test_ai_principal_requires_review() -> None:
    """An AI-kind principal always routes to review, with its declared
    owner as the sole reviewer (SPEC §9.2)."""
    decision = POLICY.evaluate(PROPOSAL, _principal("ai", owner="owner@example.com"))
    assert isinstance(decision, RequireReview)
    assert decision.reviewers == ["owner@example.com"]


def test_human_principal_auto_accepts() -> None:
    """A non-AI principal is deferred to the rest of the Composite — this
    strategy alone never blocks a human."""
    decision = POLICY.evaluate(PROPOSAL, _principal("human"))
    assert isinstance(decision, AutoAccept)


def test_service_principal_auto_accepts() -> None:
    """Only `kind == "ai"` is special-cased — a service principal is
    treated the same as human, not swept in by "not human"."""
    decision = POLICY.evaluate(PROPOSAL, _principal("service"))
    assert isinstance(decision, AutoAccept)


def test_read_capability_principal_rejected() -> None:
    """Rejects read-capability principals itself (KI-015 floor) —
    propose()/propose_ref()/retract() have no capability pre-check of
    their own, so this floor is only as strong as the installed policy."""
    decision = POLICY.evaluate(PROPOSAL, _principal("human", default_capability="read"))
    assert isinstance(decision, Reject)


def test_read_capability_ai_principal_rejected_not_reviewed() -> None:
    """The capability floor is checked before the AI-kind test — a
    read-capability AI principal is rejected outright, not routed to
    review (both mean "doesn't auto-accept", but Reject is the more
    accurate outcome for a principal that can't propose at all)."""
    decision = POLICY.evaluate(
        PROPOSAL, _principal("ai", owner="owner@example.com", default_capability="read")
    )
    assert isinstance(decision, Reject)


def test_ai_kind_never_laundered_via_acting_as() -> None:
    """An AI author delegating to a write-capable, non-AI owner still
    requires review — mirrors ThresholdPolicy's own "AI's own kind is
    never laundered away by delegating" precedent (its own evaluate()
    comment; ADR-0003 itself is scoped to accountable-owner/model-capture/
    delegation and doesn't discuss this specifically — see KI-088)."""
    ai = _principal("ai", owner="owner@example.com")
    delegate = Principal(
        id="owner@example.com",
        kind="human",
        auth_method="oidc",
        default_capability="write",
        created_at=T0,
    )

    decision = POLICY.evaluate(PROPOSAL, ai, acting_as=delegate)

    assert isinstance(decision, RequireReview)


def test_policy_is_deterministic() -> None:
    """Same inputs always produce the same decision (SPEC §9.2 purity)."""
    principal = _principal("ai", owner="owner@example.com")
    d1 = POLICY.evaluate(PROPOSAL, principal)
    d2 = POLICY.evaluate(PROPOSAL, principal)
    assert isinstance(d1, RequireReview)
    assert isinstance(d2, RequireReview)
    assert d1.reviewers == d2.reviewers


def test_policy_is_stateless() -> None:
    """Policy carries no state between evaluations."""
    ai = _principal("ai", owner="owner@example.com")
    human = _principal("human")

    d1 = POLICY.evaluate(PROPOSAL, ai)
    d2 = POLICY.evaluate(PROPOSAL, human)
    d3 = POLICY.evaluate(PROPOSAL, ai)
    assert isinstance(d1, RequireReview) and isinstance(d3, RequireReview)
    assert isinstance(d2, AutoAccept)
    assert d1.reviewers == d3.reviewers == ["owner@example.com"]


def test_composite_with_source_quorum_end_to_end(make_kb: KbFactory) -> None:
    """The exact pattern this class's own docstring documents —
    `Composite(all=[RequireReviewForAI(), SourceQuorum(...)])` — proven
    through a real `kb.propose()` call, not just hand-constructed
    Principal/Proposal objects: an AI author still requires review even
    once SourceQuorum's own threshold is met, since `all` requires every
    contained strategy to independently approve."""
    kb = make_kb(
        FixedClock(T0),
        FixedIdProvider(["e-1", "a-seed", "prop-1"]),
        policy=Composite(all=[RequireReviewForAI(), SourceQuorum(threshold=1)]),
    )
    kb.create_principal("owner@example.com", kind="human", auth_method="oidc")
    kb.create_principal(
        "bot",
        kind="ai",
        auth_method="apikey",
        owner="owner@example.com",
        default_capability="write",
    )
    entity = kb.create_entity("Person", author="owner@example.com")

    proposal, decision = kb.propose(
        entity.id, "Person.name", "Ada", "Text", "bot", source="s", model="test-model"
    )

    assert isinstance(decision, RequireReview)
    assert proposal.state == "require_review"


def test_composite_with_source_quorum_non_ai_auto_accepts_once_quorum_met(
    make_kb: KbFactory,
) -> None:
    """Same Composite, a non-AI author: RequireReviewForAI defers (AutoAccept),
    so the group's outcome depends entirely on SourceQuorum — auto-accepts
    once the configured threshold of distinct sources is reached."""
    kb = make_kb(
        FixedClock(T0),
        FixedIdProvider(["e-1", "a-seed", "prop-1"]),
        policy=Composite(all=[RequireReviewForAI(), SourceQuorum(threshold=1)]),
    )
    kb.create_principal("author", kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author="author")

    proposal, decision = kb.propose(entity.id, "Person.name", "Ada", "Text", "author", source="s")

    assert isinstance(decision, AutoAccept)
    assert proposal.state == "auto_accepted"
