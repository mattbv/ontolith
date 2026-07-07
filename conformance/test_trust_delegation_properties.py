"""Property-based tests for delegation min-capability (SPEC §8.4, govern/policy.py).

ThresholdPolicy.evaluate() is a pure function, so these tests exercise it
directly with Hypothesis-generated principal/capability pairs rather than
going through a KB. Pins the core invariant of the delegation fix: a
delegated proposal can never auto-accept on the strength of the *delegate's*
capability alone — the effective capability is always
min(capability(author), capability(acting_as)).
"""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from ontolith.govern.policy import AutoAccept, ThresholdPolicy
from ontolith.govern.proposal import Proposal
from ontolith.identity import Principal, min_capability

T0 = datetime(2025, 1, 1, tzinfo=UTC)

_capabilities = st.sampled_from(["read", "propose", "write", "review", "admin"])
_trust_levels = st.integers(min_value=0, max_value=10)


def _principal(
    id: str, kind: str, capability: str, trust_level: int, owner: str | None = None
) -> Principal:
    return Principal(
        id=id,
        kind=kind,  # type: ignore[arg-type]
        owner=owner,
        auth_method="oidc",
        default_capability=capability,  # type: ignore[arg-type]
        trust_level=trust_level,
        created_at=T0,
    )


@given(
    author_kind=st.sampled_from(["human", "service"]),
    author_cap=_capabilities,
    author_trust=_trust_levels,
    delegate_cap=_capabilities,
    delegate_trust=_trust_levels,
)
def test_delegated_auto_accept_never_exceeds_min_capability(
    author_kind: str,
    author_cap: str,
    author_trust: int,
    delegate_cap: str,
    delegate_trust: int,
) -> None:
    """AutoAccept via delegation must be justified by min(author, acting_as),
    never by the delegate's (possibly higher) capability alone."""
    policy = ThresholdPolicy()
    author = _principal("author", author_kind, author_cap, author_trust)
    delegate = _principal("delegate", "human", delegate_cap, delegate_trust)
    proposal = Proposal(id="p", namespace="default", author="author", created_at=T0)

    decision = policy.evaluate(proposal, author, acting_as=delegate)

    if isinstance(decision, AutoAccept):
        effective_cap = min_capability(author_cap, delegate_cap)
        effective_trust = min(author_trust, delegate_trust)
        # "review" only qualifies via the human write/review/admin branch, not
        # the service branch (write/admin only) — mirrors ThresholdPolicy.evaluate.
        human_capable = author_kind == "human" and effective_cap in ("write", "review", "admin")
        service_capable = author_kind == "service" and effective_cap in ("write", "admin")
        trust_elevated = effective_cap == "propose" and effective_trust >= 5
        assert human_capable or service_capable or trust_elevated


@given(
    author_cap=_capabilities,
    author_trust=_trust_levels,
    delegate_cap=_capabilities,
    delegate_trust=_trust_levels,
)
def test_ai_author_never_auto_accepts_via_delegation(
    author_cap: str,
    author_trust: int,
    delegate_cap: str,
    delegate_trust: int,
) -> None:
    """AI-kind authors always require review, regardless of the delegate's
    capability or trust level (ADR-0003) — the core delegation-bypass fix."""
    policy = ThresholdPolicy()
    author = _principal("bot", "ai", author_cap, author_trust, owner="owner@example.com")
    delegate = _principal("delegate", "human", delegate_cap, delegate_trust)
    proposal = Proposal(id="p", namespace="default", author="bot", created_at=T0)

    decision = policy.evaluate(proposal, author, acting_as=delegate)

    assert not isinstance(decision, AutoAccept)
