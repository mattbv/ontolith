"""Conformance vector: RequireReviewByRole policy strategy (SPEC §9.2, KI-069, ADR-0045).

Deeper edge-case coverage (unmapped roles, delegation not laundering role,
the KI-015 capability floor) lives in
tests/unit/test_govern.py::TestRequireReviewByRole — this file only pins
the SPEC-level contract: pure, deterministic, and that it always routes to
review with reviewers chosen by `principal.metadata["role"]`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ontolith.govern.policy import RequireReview, RequireReviewByRole
from ontolith.govern.proposal import Proposal
from ontolith.identity import Principal

T0 = datetime(2025, 1, 1, tzinfo=UTC)

POLICY = RequireReviewByRole(
    {"legal": ["legal-reviewer@example.com"], "eng": ["tech-lead@example.com"]},
    default=["fallback@example.com"],
)

PROPOSAL = Proposal(
    id="prop-001",
    namespace="default",
    author="author",
    created_at=T0,
    payload={},
)


def _principal(role: str | None) -> Principal:
    return Principal(
        id="p",
        kind="human",
        auth_method="oidc",
        default_capability="propose",
        created_at=T0,
        metadata={"role": role} if role is not None else {},
    )


def test_mapped_role_routes_to_its_own_reviewers() -> None:
    """A role present in the mapping routes to exactly its own reviewers (SPEC §9.2)."""
    decision = POLICY.evaluate(PROPOSAL, _principal("legal"))
    assert isinstance(decision, RequireReview)
    assert decision.reviewers == ["legal-reviewer@example.com"]


def test_unmapped_role_falls_back_to_default() -> None:
    """A role absent from the mapping falls back to the configured default."""
    decision = POLICY.evaluate(PROPOSAL, _principal("marketing"))
    assert isinstance(decision, RequireReview)
    assert decision.reviewers == ["fallback@example.com"]


def test_never_auto_accepts() -> None:
    """This strategy's entire purpose is routing, not approving - it never
    returns AutoAccept, even for a mapped role."""
    decision = POLICY.evaluate(PROPOSAL, _principal("eng"))
    assert isinstance(decision, RequireReview)


def test_policy_is_deterministic() -> None:
    """Same inputs always produce the same decision (SPEC §9.2 purity)."""
    principal = _principal("legal")
    d1 = POLICY.evaluate(PROPOSAL, principal)
    d2 = POLICY.evaluate(PROPOSAL, principal)
    assert isinstance(d1, RequireReview)
    assert isinstance(d2, RequireReview)
    assert d1.reviewers == d2.reviewers


def test_policy_is_stateless() -> None:
    """Policy carries no state between evaluations."""
    legal = _principal("legal")
    eng = _principal("eng")

    d1 = POLICY.evaluate(PROPOSAL, legal)
    d2 = POLICY.evaluate(PROPOSAL, eng)
    d3 = POLICY.evaluate(PROPOSAL, legal)
    assert isinstance(d1, RequireReview) and isinstance(d2, RequireReview)
    assert isinstance(d3, RequireReview)
    assert d1.reviewers == ["legal-reviewer@example.com"]
    assert d2.reviewers == ["tech-lead@example.com"]
    assert d3.reviewers == ["legal-reviewer@example.com"]
