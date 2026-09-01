"""Conformance vectors: admin-action audit trail (KI-060, SPEC §17).

Prior to this KI, the highest-stakes actions in the system — principal
creation, schema application, plugin registration, token issuance/
revocation — left no attributable trace at all. `AdminEvent` (identity/
admin_event.py) covers the first three; token issuance/revocation are
attributed directly on `PrincipalCredential.issued_by`/`.revoked_by`
instead, since every credential row already has a natural home for its own
attribution (see AdminEvent's own docstring for why).

PluginRegistry.register()'s own event recording is covered in
tests/unit/test_plugin_registry.py::TestAdminEventRecording, not here —
it isn't itself backend-parametrized the way these vectors are, and that
file already has the entry-point-patching fixtures it needs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.core.errors import CapabilityError
from ontolith.schema import ConceptDef, PropertyDef, SchemaIR

T0 = datetime(2025, 1, 1, tzinfo=UTC)

ADMIN = "admin@example.com"
OTHER_ADMIN = "carol@example.com"


def _kb_with_admin(make_kb: KbFactory) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(30)])
    kb = make_kb(clock, ids)
    kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
    return kb


def test_create_principal_with_author_records_event(make_kb: KbFactory) -> None:
    kb = _kb_with_admin(make_kb)

    kb.create_principal("alice@example.com", kind="human", default_capability="write", author=ADMIN)

    [event] = kb.backend.get_admin_events(target="alice@example.com")
    assert event.actor == ADMIN
    assert event.action == "create_principal"
    assert event.at == T0


def test_create_principal_without_author_records_no_event(make_kb: KbFactory) -> None:
    """`author=None` (the default, matching the bootstrap case where no
    admin exists yet) records nothing — capability gating stays external
    to create_principal per ADR-0022, and so does event attribution."""
    kb = _kb_with_admin(make_kb)

    kb.create_principal("alice@example.com", kind="human", default_capability="write")

    assert kb.backend.get_admin_events(target="alice@example.com") == []


def test_apply_schema_records_event(make_kb: KbFactory) -> None:
    kb = _kb_with_admin(make_kb)
    schema = SchemaIR(
        namespace="default",
        version=1,
        concepts={
            "Person": ConceptDef(
                name="Person", properties={"name": PropertyDef(name="name", value_type="Text")}
            )
        },
    )

    kb.apply_schema(schema, author=ADMIN)

    [event] = kb.backend.get_admin_events(actor=ADMIN)
    assert event.action == "apply_schema"
    assert event.target == "default:v1"


def test_apply_schema_event_and_write_share_one_transaction(make_kb: KbFactory) -> None:
    """A capability failure aborts before either write happens - no
    partial state where the schema applied but no event was recorded, or
    vice versa."""
    kb = _kb_with_admin(make_kb)
    kb.create_principal("alice@example.com", kind="human", default_capability="write")
    schema = SchemaIR(namespace="default", version=1, concepts={})

    with pytest.raises(CapabilityError):
        kb.apply_schema(schema, author="alice@example.com")

    assert kb.backend.get_schema("default") is None
    assert kb.backend.get_admin_events() == []


def test_issue_token_records_issued_by(make_kb: KbFactory) -> None:
    kb = _kb_with_admin(make_kb)
    kb.create_principal("alice@example.com", kind="human", default_capability="write")

    _token, credential_id = kb.issue_token("alice@example.com", author=ADMIN)

    credential = kb.backend.get_credential(credential_id)
    assert credential is not None
    assert credential.issued_by == ADMIN
    assert credential.revoked_by is None


def test_revoke_token_records_revoked_by(make_kb: KbFactory) -> None:
    kb = _kb_with_admin(make_kb)
    kb.create_principal("alice@example.com", kind="human", default_capability="write")
    _token, credential_id = kb.issue_token("alice@example.com", author=ADMIN)

    kb.revoke_token(credential_id, author=ADMIN)

    credential = kb.backend.get_credential(credential_id)
    assert credential is not None
    assert credential.revoked_by == ADMIN


def test_re_revoking_does_not_launder_original_revoker(make_kb: KbFactory) -> None:
    """A second admin calling revoke_token on an already-revoked credential
    must not silently overwrite who actually revoked it first (KI-060's
    whole point - this would otherwise be exactly the attribution
    laundering the audit trail exists to prevent)."""
    kb = _kb_with_admin(make_kb)
    kb.create_principal(OTHER_ADMIN, kind="human", default_capability="admin")
    kb.create_principal("alice@example.com", kind="human", default_capability="write")
    _token, credential_id = kb.issue_token("alice@example.com", author=ADMIN)

    kb.revoke_token(credential_id, author=ADMIN)
    kb.revoke_token(credential_id, author=OTHER_ADMIN)  # no-op, doesn't raise

    credential = kb.backend.get_credential(credential_id)
    assert credential is not None
    assert credential.revoked_by == ADMIN


def test_get_admin_events_filters_by_actor_and_target(make_kb: KbFactory) -> None:
    kb = _kb_with_admin(make_kb)
    kb.create_principal(OTHER_ADMIN, kind="human", default_capability="admin")
    kb.create_principal("alice@example.com", kind="human", default_capability="write", author=ADMIN)
    kb.create_principal(
        "bob@example.com", kind="human", default_capability="write", author=OTHER_ADMIN
    )

    by_admin = kb.backend.get_admin_events(actor=ADMIN)
    assert {e.target for e in by_admin} == {"alice@example.com"}

    by_target = kb.backend.get_admin_events(target="bob@example.com")
    assert {e.actor for e in by_target} == {OTHER_ADMIN}
