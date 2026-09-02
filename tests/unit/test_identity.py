"""Unit tests for the identity layer's token-based auth (ADR-0014)."""

import tempfile
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core.errors import AuthError, CapabilityError
from ontolith.identity.token_auth import TokenAuthProvider, hash_token

ADMIN = "admin@example.com"


@pytest.fixture
def kb() -> Ontology:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    kb = Ontology.connect(path)
    kb.create_principal("alice@example.com", kind="human", default_capability="write")
    kb.create_principal(ADMIN, kind="human", default_capability="admin")
    yield kb
    kb.close()
    path.unlink()


class TestHashToken:
    def test_same_input_hashes_identically(self) -> None:
        assert hash_token("secret") == hash_token("secret")

    def test_different_input_hashes_differently(self) -> None:
        assert hash_token("secret-a") != hash_token("secret-b")

    def test_hash_is_not_the_raw_token(self) -> None:
        assert hash_token("secret") != "secret"


class TestTokenAuthProvider:
    def test_resolve_returns_correct_principal(self, kb: Ontology) -> None:
        token, _ = kb.issue_token("alice@example.com", author=ADMIN)
        provider = TokenAuthProvider(kb.backend)

        principal = provider.resolve(token)

        assert principal.id == "alice@example.com"

    def test_resolve_unknown_token_raises_auth_error(self, kb: Ontology) -> None:
        provider = TokenAuthProvider(kb.backend)
        with pytest.raises(AuthError, match="Invalid or revoked token"):
            provider.resolve("garbage-token")

    def test_resolve_revoked_token_raises_auth_error(self, kb: Ontology) -> None:
        token, credential_id = kb.issue_token("alice@example.com", author=ADMIN)
        kb.revoke_token(credential_id, author=ADMIN)

        provider = TokenAuthProvider(kb.backend)
        with pytest.raises(AuthError, match="Invalid or revoked token"):
            provider.resolve(token)

    def test_two_tokens_both_resolve_independently(self, kb: Ontology) -> None:
        token_a, _ = kb.issue_token("alice@example.com", author=ADMIN)
        token_b, _ = kb.issue_token("alice@example.com", author=ADMIN)
        provider = TokenAuthProvider(kb.backend)

        assert provider.resolve(token_a).id == "alice@example.com"
        assert provider.resolve(token_b).id == "alice@example.com"

    def test_revoking_one_token_does_not_invalidate_the_other(self, kb: Ontology) -> None:
        token_a, _ = kb.issue_token("alice@example.com", author=ADMIN)
        token_b, _ = kb.issue_token("alice@example.com", author=ADMIN)
        credentials = kb.list_tokens("alice@example.com", author=ADMIN)
        # Revoke whichever credential corresponds to token_a
        provider = TokenAuthProvider(kb.backend)
        cred_a = next(c for c in credentials if hash_token(token_a) == c.token_hash)
        kb.revoke_token(cred_a.id, author=ADMIN)

        with pytest.raises(AuthError):
            provider.resolve(token_a)
        assert provider.resolve(token_b).id == "alice@example.com"


class TestOntologyTokenIssuance:
    def test_issue_token_raw_value_not_the_stored_hash(self, kb: Ontology) -> None:
        token, _ = kb.issue_token("alice@example.com", author=ADMIN)
        credential = kb.list_tokens("alice@example.com", author=ADMIN)[0]
        assert token != credential.token_hash

    def test_issue_token_returns_the_credential_id_it_just_created(self, kb: Ontology) -> None:
        """KI-024: the returned credential_id must be issue_token's own new
        credential, not re-derived via a second, racy list_tokens() lookup."""
        _, credential_id = kb.issue_token("alice@example.com", author=ADMIN)
        credential = kb.list_tokens("alice@example.com", author=ADMIN)[0]
        assert credential_id == credential.id

    def test_issue_token_unknown_principal_raises_auth_error(self, kb: Ontology) -> None:
        with pytest.raises(AuthError, match="Principal not found"):
            kb.issue_token("nobody@example.com", author=ADMIN)

    def test_issue_token_round_trips_through_auth_provider(self, kb: Ontology) -> None:
        token, _ = kb.issue_token("alice@example.com", author=ADMIN)
        principal = TokenAuthProvider(kb.backend).resolve(token)
        assert principal.id == "alice@example.com"

    def test_revoke_token_unknown_credential_raises_not_found(self, kb: Ontology) -> None:
        from ontolith.core.errors import NotFoundError

        with pytest.raises(NotFoundError, match="Token credential not found"):
            kb.revoke_token("nonexistent", author=ADMIN)

    def test_list_tokens_reflects_issuance_and_revocation(self, kb: Ontology) -> None:
        kb.issue_token("alice@example.com", author=ADMIN)
        kb.issue_token("alice@example.com", author=ADMIN)
        credentials = kb.list_tokens("alice@example.com", author=ADMIN)
        assert len(credentials) == 2
        assert all(c.revoked_at is None for c in credentials)

        kb.revoke_token(credentials[0].id, author=ADMIN)
        refreshed = kb.list_tokens("alice@example.com", author=ADMIN)
        revoked = next(c for c in refreshed if c.id == credentials[0].id)
        assert revoked.revoked_at is not None

    def test_list_tokens_empty_for_principal_with_no_credentials(self, kb: Ontology) -> None:
        assert kb.list_tokens("alice@example.com", author=ADMIN) == []


class TestOntologyListPrincipals:
    """KI-022: Ontology.list_principals(), the SDK method REST's GET /principals wraps."""

    def test_list_principals_includes_seeded_and_created(self, kb: Ontology) -> None:
        ids = {p.id for p in kb.list_principals(author=ADMIN)}
        assert ids == {"alice@example.com", ADMIN}

    def test_list_principals_rejects_non_admin_author(self, kb: Ontology) -> None:
        with pytest.raises(CapabilityError, match="lacks admin capability"):
            kb.list_principals(author="alice@example.com")

    def test_list_principals_rejects_unknown_author(self, kb: Ontology) -> None:
        with pytest.raises(AuthError, match="Principal not found"):
            kb.list_principals(author="nobody@example.com")


class TestTokenIssuanceRequiresAdmin:
    """Issuing/revoking/listing credentials converts local access into a
    remote, network-reachable bearer token — a higher-stakes action than
    the target principal's own capability, so it requires an admin author."""

    def test_issue_token_rejects_non_admin_author(self, kb: Ontology) -> None:
        with pytest.raises(CapabilityError, match="lacks admin capability"):
            kb.issue_token("alice@example.com", author="alice@example.com")

    def test_issue_token_rejects_unknown_author(self, kb: Ontology) -> None:
        with pytest.raises(AuthError, match="Principal not found"):
            kb.issue_token("alice@example.com", author="nobody@example.com")

    def test_revoke_token_rejects_non_admin_author(self, kb: Ontology) -> None:
        _, credential_id = kb.issue_token("alice@example.com", author=ADMIN)
        with pytest.raises(CapabilityError, match="lacks admin capability"):
            kb.revoke_token(credential_id, author="alice@example.com")

    def test_list_tokens_rejects_non_admin_author(self, kb: Ontology) -> None:
        with pytest.raises(CapabilityError, match="lacks admin capability"):
            kb.list_tokens("alice@example.com", author="alice@example.com")


class TestOntologyGetAdminEvents:
    """KI-072: Ontology.get_admin_events(), the read half of KI-060's audit
    trail — REST's GET /admin-events and the CLI's `admin-events list`
    both wrap this."""

    def test_get_admin_events_reflects_create_principal(self, kb: Ontology) -> None:
        kb.create_principal(
            "bob@example.com", kind="human", default_capability="write", author=ADMIN
        )

        events = kb.get_admin_events(author=ADMIN)

        [event] = [e for e in events if e.target == "bob@example.com"]
        assert event.actor == ADMIN
        assert event.action == "create_principal"

    def test_get_admin_events_filters_by_actor_and_target(self, kb: Ontology) -> None:
        kb.create_principal(
            "carol@example.com", kind="human", default_capability="admin", author=ADMIN
        )
        kb.create_principal(
            "dave@example.com", kind="human", default_capability="write", author="carol@example.com"
        )

        by_actor = kb.get_admin_events(author=ADMIN, actor="carol@example.com")
        assert [e.target for e in by_actor] == ["dave@example.com"]

        by_target = kb.get_admin_events(author=ADMIN, target="carol@example.com")
        assert [e.actor for e in by_target] == [ADMIN]

    def test_get_admin_events_returns_oldest_first(self, kb: Ontology) -> None:
        """Docstring-promised ordering (matching the backend's own ORDER BY
        at ASC, id ASC) - REST/CLI both ship this order unchanged, so a
        regression here would silently reach both."""
        kb.create_principal(
            "carol@example.com", kind="human", default_capability="write", author=ADMIN
        )
        kb.create_principal(
            "dave@example.com", kind="human", default_capability="write", author=ADMIN
        )
        kb.create_principal(
            "erin@example.com", kind="human", default_capability="write", author=ADMIN
        )

        events = kb.get_admin_events(author=ADMIN)

        targets = [e.target for e in events]
        assert targets.index("carol@example.com") < targets.index("dave@example.com")
        assert targets.index("dave@example.com") < targets.index("erin@example.com")

    def test_get_admin_events_empty_when_none_recorded(self, kb: Ontology) -> None:
        assert kb.get_admin_events(author=ADMIN) == []

    def test_get_admin_events_rejects_non_admin_author(self, kb: Ontology) -> None:
        with pytest.raises(CapabilityError, match="lacks admin capability"):
            kb.get_admin_events(author="alice@example.com")

    def test_get_admin_events_rejects_unknown_author(self, kb: Ontology) -> None:
        with pytest.raises(AuthError, match="Principal not found"):
            kb.get_admin_events(author="nobody@example.com")
