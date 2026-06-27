"""Conformance vector: Proposal policy evaluation.

SPEC §9.2 — Policy is a pure function: deterministic, no I/O.
ThresholdPolicy MUST return AutoAccept for trusted principals and
RequireReview for AI principals or low-capability principals.
"""

from datetime import UTC, datetime

from ontolith.govern.policy import AutoAccept, RequireReview, ThresholdPolicy
from ontolith.govern.proposal import Proposal
from ontolith.identity import Principal

POLICY = ThresholdPolicy()

T0 = datetime(2025, 1, 1, tzinfo=UTC)

PROPOSAL = Proposal(
    id="prop-001",
    namespace="default",
    author="author",
    created_at=T0,
    payload={"op": "assert", "predicate": "Person.name", "value": "Ada"},
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


def test_human_write_capability_auto_accepts() -> None:
    """Human principal with write capability is auto-accepted (SPEC §9.2)."""
    principal = _principal(default_capability="write")
    decision = POLICY.evaluate(PROPOSAL, principal)
    assert isinstance(decision, AutoAccept)
    assert "human" in decision.reason.lower() or principal.id in decision.reason


def test_human_review_capability_auto_accepts() -> None:
    """Human principal with review capability is auto-accepted."""
    principal = _principal(default_capability="review")
    decision = POLICY.evaluate(PROPOSAL, principal)
    assert isinstance(decision, AutoAccept)


def test_human_admin_capability_auto_accepts() -> None:
    """Human principal with admin capability is auto-accepted."""
    principal = _principal(default_capability="admin")
    decision = POLICY.evaluate(PROPOSAL, principal)
    assert isinstance(decision, AutoAccept)


def test_human_propose_capability_requires_review() -> None:
    """Human principal with only propose capability requires review."""
    principal = _principal(default_capability="propose")
    decision = POLICY.evaluate(PROPOSAL, principal)
    assert isinstance(decision, RequireReview)


def test_human_read_capability_requires_review() -> None:
    """Human principal with read capability requires review."""
    principal = _principal(default_capability="read")
    decision = POLICY.evaluate(PROPOSAL, principal)
    assert isinstance(decision, RequireReview)


def test_ai_principal_always_requires_review() -> None:
    """AI principals always require review in M1 (SPEC §9.2)."""
    principal = _principal(
        id="scout-agent",
        kind="ai",
        auth_method="workload",
        owner="alice@example.com",
        default_capability="write",
    )
    decision = POLICY.evaluate(PROPOSAL, principal)
    assert isinstance(decision, RequireReview)
    assert "alice@example.com" in decision.reviewers


def test_ai_review_routes_to_owner() -> None:
    """AI proposal review is routed to the accountable owner (ADR-0003)."""
    owner = "bob@example.com"
    principal = _principal(
        id="research-bot",
        kind="ai",
        auth_method="workload",
        owner=owner,
        default_capability="propose",
    )
    decision = POLICY.evaluate(PROPOSAL, principal)
    assert isinstance(decision, RequireReview)
    assert owner in decision.reviewers


def test_service_write_capability_auto_accepts() -> None:
    """Service principal with write capability is auto-accepted."""
    principal = _principal(
        id="etl-service",
        kind="service",
        auth_method="apikey",
        default_capability="write",
    )
    decision = POLICY.evaluate(PROPOSAL, principal)
    assert isinstance(decision, AutoAccept)


def test_service_admin_capability_auto_accepts() -> None:
    """Service principal with admin capability is auto-accepted."""
    principal = _principal(
        id="etl-service",
        kind="service",
        auth_method="apikey",
        default_capability="admin",
    )
    decision = POLICY.evaluate(PROPOSAL, principal)
    assert isinstance(decision, AutoAccept)


def test_service_propose_capability_requires_review() -> None:
    """Service principal with propose capability requires review."""
    principal = _principal(
        id="etl-service",
        kind="service",
        auth_method="apikey",
        default_capability="propose",
    )
    decision = POLICY.evaluate(PROPOSAL, principal)
    assert isinstance(decision, RequireReview)


def test_policy_is_deterministic() -> None:
    """Same inputs always produce the same decision (SPEC §9.2 purity)."""
    principal = _principal(default_capability="write")
    d1 = POLICY.evaluate(PROPOSAL, principal)
    d2 = POLICY.evaluate(PROPOSAL, principal)
    assert type(d1) is type(d2)
    assert d1.reason == d2.reason


def test_policy_is_stateless() -> None:
    """Policy carries no state between evaluations."""
    p_write = _principal(id="writer", default_capability="write")
    p_propose = _principal(id="proposer", default_capability="propose")

    # Interleave evaluations — result must not depend on call order.
    assert isinstance(POLICY.evaluate(PROPOSAL, p_write), AutoAccept)
    assert isinstance(POLICY.evaluate(PROPOSAL, p_propose), RequireReview)
    assert isinstance(POLICY.evaluate(PROPOSAL, p_write), AutoAccept)
    assert isinstance(POLICY.evaluate(PROPOSAL, p_propose), RequireReview)
