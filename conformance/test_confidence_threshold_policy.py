"""Conformance vector: ConfidenceThreshold policy strategy (SPEC §9.2, KI-069, ADR-0045).

Deeper edge-case coverage (retractions, unrecognized op kinds, missing
confidence, delegation, the KI-015 capability floor) lives in
tests/unit/test_govern.py::TestConfidenceThreshold — this file only pins
the SPEC-level contract: pure, deterministic, and the core
accept/review split on the proposal's own staged confidence.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ontolith.govern.policy import AutoAccept, ConfidenceThreshold, RequireReview
from ontolith.govern.proposal import Proposal
from ontolith.identity import Principal

POLICY = ConfidenceThreshold(threshold=0.8, reviewers=["reviewer@example.com"])

T0 = datetime(2025, 1, 1, tzinfo=UTC)

PRINCIPAL = Principal(
    id="author",
    kind="human",
    auth_method="oidc",
    default_capability="propose",
    created_at=T0,
)


def _proposal(confidence: float | None) -> Proposal:
    return Proposal(
        id="prop-001",
        namespace="default",
        author=PRINCIPAL.id,
        created_at=T0,
        payload={
            "operations": [
                {
                    "kind": "assert_literal",
                    "subject": "e-1",
                    "predicate": "Person.name",
                    "value": "Ada",
                    "value_type": "Text",
                    "confidence": confidence,
                }
            ]
        },
    )


def test_confidence_meeting_threshold_auto_accepts() -> None:
    """Confidence >= threshold auto-accepts (SPEC §9.2)."""
    decision = POLICY.evaluate(_proposal(0.9), PRINCIPAL)
    assert isinstance(decision, AutoAccept)


def test_confidence_below_threshold_requires_review() -> None:
    """Confidence < threshold requires review, with the configured reviewers."""
    decision = POLICY.evaluate(_proposal(0.5), PRINCIPAL)
    assert isinstance(decision, RequireReview)
    assert decision.reviewers == ["reviewer@example.com"]


def test_missing_confidence_requires_review() -> None:
    """A proposal with no confidence never auto-accepts (never assumes a default)."""
    decision = POLICY.evaluate(_proposal(None), PRINCIPAL)
    assert isinstance(decision, RequireReview)


def test_policy_is_deterministic() -> None:
    """Same inputs always produce the same decision (SPEC §9.2 purity)."""
    d1 = POLICY.evaluate(_proposal(0.9), PRINCIPAL)
    d2 = POLICY.evaluate(_proposal(0.9), PRINCIPAL)
    assert type(d1) is type(d2)
    assert d1.reason == d2.reason


def test_policy_is_stateless() -> None:
    """Policy carries no state between evaluations."""
    assert isinstance(POLICY.evaluate(_proposal(0.9), PRINCIPAL), AutoAccept)
    assert isinstance(POLICY.evaluate(_proposal(0.5), PRINCIPAL), RequireReview)
    assert isinstance(POLICY.evaluate(_proposal(0.9), PRINCIPAL), AutoAccept)
