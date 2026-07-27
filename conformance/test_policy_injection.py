"""Conformance vector: PolicyStrategy injection (ADR-0018).

Ontology accepts a custom `policy: PolicyStrategy` instead of hardcoding
ThresholdPolicy — proving the ADR-0006 open-core extension point actually
works, not just that it's declared. All three governed write paths
(propose, propose_ref, retract) must consult the injected policy.
"""

from __future__ import annotations

from datetime import UTC, datetime

from conformance.conftest import KbFactory
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.govern.policy import AutoAccept, Decision, KbView, Reject
from ontolith.identity import Principal

T0 = datetime(2025, 1, 1, tzinfo=UTC)
AUTHOR = "alice@example.com"


class _AlwaysReject:
    """Custom PolicyStrategy that rejects every proposal, regardless of
    the author's capability — proves the injected policy is actually
    consulted, not just accepted and ignored (ThresholdPolicy would
    auto-accept a write-capability human's proposal)."""

    def evaluate(
        self,
        proposal: object,
        principal: Principal,
        kb: KbView,
        acting_as: Principal | None = None,
    ) -> Decision:
        return Reject(reason="custom policy: always reject")


class _AlwaysAutoAccept:
    """Custom PolicyStrategy that auto-accepts every proposal, regardless
    of capability — proves injection in the opposite direction (an author
    ThresholdPolicy would normally route to require_review)."""

    def evaluate(
        self,
        proposal: object,
        principal: Principal,
        kb: KbView,
        acting_as: Principal | None = None,
    ) -> Decision:
        return AutoAccept(reason="custom policy: always accept")


def test_custom_policy_overrides_default_reject(make_kb: KbFactory) -> None:
    """A write-capability human's proposal auto-accepts under the default
    ThresholdPolicy - injecting _AlwaysReject must instead reject it."""
    kb = make_kb(FixedClock(T0), FixedIdProvider(["e-1", "a-1", "prop-1"]), policy=_AlwaysReject())
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author=AUTHOR)

    proposal, decision = kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)

    assert isinstance(decision, Reject)
    assert proposal.state == "rejected"
    assert kb.assertions(subject=entity.id, predicate="Person.name") == []


def test_custom_policy_overrides_default_require_review(make_kb: KbFactory) -> None:
    """An AI principal's proposal always requires review under the default
    ThresholdPolicy (ADR-0003) - injecting _AlwaysAutoAccept must instead
    accept it immediately."""
    kb = make_kb(
        FixedClock(T0), FixedIdProvider(["e-1", "a-1", "prop-1"]), policy=_AlwaysAutoAccept()
    )
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(
        "bot@example.com",
        kind="ai",
        auth_method="apikey",
        owner=AUTHOR,
        default_capability="propose",
    )
    entity = kb.create_entity("Person", author=AUTHOR)

    proposal, decision = kb.propose(
        entity.id, "Person.name", "Ada", "Text", "bot@example.com", model="test-model-v1"
    )

    assert isinstance(decision, AutoAccept)
    assert proposal.state == "auto_accepted"
    [assertion] = kb.assertions(subject=entity.id, predicate="Person.name")
    assert assertion.value == "Ada"


def test_default_policy_is_threshold_policy_when_unspecified(make_kb: KbFactory) -> None:
    """Regression guard: omitting policy= keeps today's ThresholdPolicy
    default behavior (AI always requires review)."""
    kb = make_kb(FixedClock(T0), FixedIdProvider(["e-1", "p-1", "a-1", "prop-1"]))
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(
        "bot@example.com",
        kind="ai",
        auth_method="apikey",
        owner=AUTHOR,
        default_capability="propose",
    )
    entity = kb.create_entity("Person", author=AUTHOR)

    proposal, _ = kb.propose(
        entity.id, "Person.name", "Ada", "Text", "bot@example.com", model="test-model-v1"
    )
    assert proposal.state == "require_review"


def test_retract_consults_injected_policy(make_kb: KbFactory) -> None:
    """retract() also evaluates self.policy, not just propose()/propose_ref()."""
    kb = make_kb(
        FixedClock(T0), FixedIdProvider(["e-1", "a-1", "prop-1", "prop-2"]), policy=_AlwaysReject()
    )
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author=AUTHOR)
    assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)

    proposal, decision = kb.retract(assertion.id, AUTHOR)

    assert isinstance(decision, Reject)
    assert kb.assertions(subject=entity.id, predicate="Person.name", status="active") != []
