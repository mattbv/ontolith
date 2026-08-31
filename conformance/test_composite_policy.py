"""Conformance vector: Composite policy strategy (SPEC §9.2, KI-061, ADR-0040).

Pure decision-combination tests use fake `PolicyStrategy` stubs that return
a fixed `Decision` regardless of input (no KB needed — Composite's own
combination logic is what's under test). The final test exercises the
actual KI-061 motivating scenario end-to-end against a real backend:
`SourceQuorum` (which deliberately does not special-case AI authorship,
ADR-0025 §5) composed with a small AI-review strategy via `Composite`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conformance.conftest import KbFactory
from ontolith.core import Assertion, FixedClock, FixedIdProvider
from ontolith.govern.policy import (
    AutoAccept,
    Composite,
    Decision,
    Reject,
    RequireReview,
    SourceQuorum,
)
from ontolith.govern.proposal import Proposal
from ontolith.identity import Principal

T0 = datetime(2025, 1, 1, tzinfo=UTC)

PROPOSAL = Proposal(
    id="prop-001",
    namespace="default",
    author="author",
    created_at=T0,
    payload={"operations": []},
)


def _principal(**kwargs) -> Principal:
    defaults: dict = {
        "id": "p",
        "kind": "human",
        "auth_method": "oidc",
        "default_capability": "propose",
        "trust_level": 0,
        "created_at": T0,
    }
    defaults.update(kwargs)
    return Principal(**defaults)  # type: ignore[arg-type]


PRINCIPAL = _principal()


class _EmptyKb:
    """Minimal KbView stub - Composite's own `kb` parameter is required
    (it may forward to a contained strategy that genuinely reads it, e.g.
    SourceQuorum), but none of the fake strategies below actually read it."""

    def assertions(
        self, subject: str | None = None, predicate: str | None = None
    ) -> list[Assertion]:
        return []


EMPTY_KB = _EmptyKb()


class _Fixed:
    """PolicyStrategy stub that always returns the same decision, ignoring
    every input — isolates Composite's own combination logic from any real
    strategy's evaluation rules."""

    def __init__(self, decision: Decision) -> None:
        self.decision = decision

    def evaluate(self, proposal, principal, kb=None, acting_as=None) -> Decision:
        return self.decision


def test_empty_composite_raises() -> None:
    """A Composite with nothing to compose is a configuration mistake."""
    with pytest.raises(ValueError, match="at least one strategy"):
        Composite()


def test_groups_are_not_publicly_mutable() -> None:
    """Review finding: an earlier version stored the `all`/`any` groups as
    public mutable lists, so `composite.all.clear()` could silently empty
    a group after construction and make `evaluate()` fail open despite
    `__init__`'s guard against an empty Composite. No public `all`/`any`
    attribute exists to mutate anymore."""
    policy = Composite(all=[_Fixed(AutoAccept("ok"))])
    assert not hasattr(policy, "all")
    assert not hasattr(policy, "any")


def test_all_auto_accepts_when_every_strategy_does() -> None:
    policy = Composite(all=[_Fixed(AutoAccept("a")), _Fixed(AutoAccept("b"))])
    decision = policy.evaluate(PROPOSAL, PRINCIPAL, EMPTY_KB)
    assert isinstance(decision, AutoAccept)
    assert decision.reason == "a; b"


def test_all_require_review_overrides_auto_accept() -> None:
    """One RequireReview in the `all` group blocks auto-accept even though
    another strategy in the same group would have approved — this is the
    exact shape KI-061's motivating scenario needs."""
    policy = Composite(
        all=[
            _Fixed(RequireReview(["carol"], "needs review")),
            _Fixed(AutoAccept("quorum reached")),
        ]
    )
    decision = policy.evaluate(PROPOSAL, PRINCIPAL, EMPTY_KB)
    assert isinstance(decision, RequireReview)
    assert decision.reviewers == ["carol"]
    assert decision.reason == "needs review"


def test_all_reject_overrides_require_review_and_auto_accept() -> None:
    """Reject is the most restrictive severity — it wins over both other
    decisions in the same `all` group."""
    policy = Composite(
        all=[
            _Fixed(AutoAccept("fine")),
            _Fixed(RequireReview([], "needs review")),
            _Fixed(Reject("no")),
        ]
    )
    decision = policy.evaluate(PROPOSAL, PRINCIPAL, EMPTY_KB)
    assert isinstance(decision, Reject)
    assert decision.reason == "no"


def test_all_merges_same_severity_reviewers_deduped() -> None:
    policy = Composite(
        all=[
            _Fixed(RequireReview(["carol", "dave"], "reason-1")),
            _Fixed(RequireReview(["dave", "erin"], "reason-2")),
        ]
    )
    decision = policy.evaluate(PROPOSAL, PRINCIPAL, EMPTY_KB)
    assert isinstance(decision, RequireReview)
    # Dedup'd union, first-seen order — "dave" named by both isn't doubled.
    assert decision.reviewers == ["carol", "dave", "erin"]
    assert decision.reason == "reason-1; reason-2"


def test_any_auto_accepts_when_one_strategy_does() -> None:
    """Only one strategy in the `any` group needs to approve."""
    policy = Composite(
        any=[
            _Fixed(RequireReview([], "not this one")),
            _Fixed(AutoAccept("this one says yes")),
        ]
    )
    decision = policy.evaluate(PROPOSAL, PRINCIPAL, EMPTY_KB)
    assert isinstance(decision, AutoAccept)
    assert decision.reason == "this one says yes"


def test_any_requires_review_when_none_auto_accept() -> None:
    policy = Composite(
        any=[
            _Fixed(RequireReview(["carol"], "reason-1")),
            _Fixed(RequireReview(["dave"], "reason-2")),
        ]
    )
    decision = policy.evaluate(PROPOSAL, PRINCIPAL, EMPTY_KB)
    assert isinstance(decision, RequireReview)
    assert decision.reviewers == ["carol", "dave"]


def test_any_rejects_only_when_every_strategy_rejects() -> None:
    policy = Composite(any=[_Fixed(Reject("no-1")), _Fixed(Reject("no-2"))])
    decision = policy.evaluate(PROPOSAL, PRINCIPAL, EMPTY_KB)
    assert isinstance(decision, Reject)
    assert decision.reason == "no-1; no-2"


def test_unset_group_does_not_constrain() -> None:
    """A Composite with only `all=` set behaves exactly as that group alone
    would - the unset `any` group doesn't drag the result down."""
    policy = Composite(all=[_Fixed(AutoAccept("ok"))])
    decision = policy.evaluate(PROPOSAL, PRINCIPAL, EMPTY_KB)
    assert isinstance(decision, AutoAccept)


def test_both_groups_must_approve() -> None:
    """AutoAccept from `all` doesn't override a RequireReview verdict from
    `any` - both groups' constraints apply jointly."""
    policy = Composite(
        all=[_Fixed(AutoAccept("all group is fine"))],
        any=[_Fixed(RequireReview(["carol"], "any group isn't satisfied"))],
    )
    decision = policy.evaluate(PROPOSAL, PRINCIPAL, EMPTY_KB)
    assert isinstance(decision, RequireReview)
    assert decision.reviewers == ["carol"]


def test_is_deterministic() -> None:
    """Same inputs always produce the same decision (SPEC §9.2 purity)."""
    policy = Composite(all=[_Fixed(AutoAccept("ok"))], any=[_Fixed(AutoAccept("also ok"))])
    d1 = policy.evaluate(PROPOSAL, PRINCIPAL, EMPTY_KB)
    d2 = policy.evaluate(PROPOSAL, PRINCIPAL, EMPTY_KB)
    assert type(d1) is type(d2)
    assert d1.reason == d2.reason


class _Spy:
    """PolicyStrategy stub that records the exact (kb, acting_as) it was
    called with, then auto-accepts - proves Composite forwards both
    arguments to every contained strategy rather than dropping them."""

    def __init__(self) -> None:
        self.received_kb: object | None = None
        self.received_acting_as: Principal | None = None

    def evaluate(self, proposal, principal, kb=None, acting_as=None) -> Decision:
        self.received_kb = kb
        self.received_acting_as = acting_as
        return AutoAccept("spy")


def test_kb_and_acting_as_are_forwarded_to_every_contained_strategy() -> None:
    delegate = _principal(id="delegate")
    all_spy = _Spy()
    any_spy = _Spy()
    policy = Composite(all=[all_spy], any=[any_spy])

    policy.evaluate(PROPOSAL, PRINCIPAL, EMPTY_KB, acting_as=delegate)

    assert all_spy.received_kb is EMPTY_KB
    assert all_spy.received_acting_as is delegate
    assert any_spy.received_kb is EMPTY_KB
    assert any_spy.received_acting_as is delegate


class _RequireReviewForAI:
    """The minimal AI-review strategy documented as an example in
    Composite's own docstring - reproduced here (not imported, since it's
    intentionally not a shipped symbol, ADR-0040) to test the actual
    KI-061 motivating scenario end-to-end."""

    def evaluate(self, proposal, principal, kb=None, acting_as=None) -> Decision:
        if principal.kind == "ai":
            return RequireReview(
                [principal.owner] if principal.owner else [], "AI proposals require review"
            )
        return AutoAccept("non-AI: deferring to the rest of the Composite")


def test_composite_closes_the_ki_061_gap(make_kb: KbFactory) -> None:
    """SourceQuorum alone lets an AI-authored proposal auto-accept once
    quorum is reached (ADR-0025 §5, pinned separately in
    test_source_quorum_policy.py::test_ai_authored_proposal_can_auto_accept
    — unchanged by this test). Composed with an AI-review strategy via
    Composite, the same scenario now requires review instead."""
    kb = make_kb(
        FixedClock(T0),
        FixedIdProvider(["e-1", "a-seed", "prop-1"]),
        policy=Composite(all=[_RequireReviewForAI(), SourceQuorum(threshold=2)]),
    )
    author = "alice@example.com"
    kb.create_principal(author, kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author=author)
    kb.assert_literal(entity.id, "Person.name", "Ada", "Text", author, source="source-a")
    bot = "bot@example.com"
    kb.create_principal(
        bot, kind="ai", auth_method="apikey", owner=author, default_capability="propose"
    )

    proposal, decision = kb.propose(
        entity.id, "Person.name", "Ada", "Text", bot, source="source-b", model="test-model-v1"
    )

    # Quorum of 2 IS reached (source-a + source-b) - SourceQuorum alone
    # would auto-accept. The composed AI-review strategy blocks it anyway.
    assert isinstance(decision, RequireReview)
    assert proposal.state == "require_review"
    assert author in decision.reviewers
