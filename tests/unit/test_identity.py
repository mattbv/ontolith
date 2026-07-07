"""Unit tests for the identity layer's token-based auth (ADR-0014)."""

import tempfile
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core.errors import AuthError
from ontolith.identity.token_auth import TokenAuthProvider, hash_token


@pytest.fixture
def kb() -> Ontology:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    kb = Ontology.connect(path)
    kb.create_principal("alice@example.com", kind="human", default_capability="write")
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
        token = kb.issue_token("alice@example.com")
        provider = TokenAuthProvider(kb.backend)

        principal = provider.resolve(token)

        assert principal.id == "alice@example.com"

    def test_resolve_unknown_token_raises_auth_error(self, kb: Ontology) -> None:
        provider = TokenAuthProvider(kb.backend)
        with pytest.raises(AuthError, match="Invalid or revoked token"):
            provider.resolve("garbage-token")

    def test_resolve_revoked_token_raises_auth_error(self, kb: Ontology) -> None:
        token = kb.issue_token("alice@example.com")
        credential_id = kb.list_tokens("alice@example.com")[0].id
        kb.revoke_token(credential_id)

        provider = TokenAuthProvider(kb.backend)
        with pytest.raises(AuthError, match="Invalid or revoked token"):
            provider.resolve(token)

    def test_two_tokens_both_resolve_independently(self, kb: Ontology) -> None:
        token_a = kb.issue_token("alice@example.com")
        token_b = kb.issue_token("alice@example.com")
        provider = TokenAuthProvider(kb.backend)

        assert provider.resolve(token_a).id == "alice@example.com"
        assert provider.resolve(token_b).id == "alice@example.com"

    def test_revoking_one_token_does_not_invalidate_the_other(self, kb: Ontology) -> None:
        token_a = kb.issue_token("alice@example.com")
        token_b = kb.issue_token("alice@example.com")
        credentials = kb.list_tokens("alice@example.com")
        # Revoke whichever credential corresponds to token_a
        provider = TokenAuthProvider(kb.backend)
        cred_a = next(c for c in credentials if hash_token(token_a) == c.token_hash)
        kb.revoke_token(cred_a.id)

        with pytest.raises(AuthError):
            provider.resolve(token_a)
        assert provider.resolve(token_b).id == "alice@example.com"


class TestOntologyTokenIssuance:
    def test_issue_token_raw_value_not_the_stored_hash(self, kb: Ontology) -> None:
        token = kb.issue_token("alice@example.com")
        credential = kb.list_tokens("alice@example.com")[0]
        assert token != credential.token_hash

    def test_issue_token_unknown_principal_raises_auth_error(self, kb: Ontology) -> None:
        with pytest.raises(AuthError, match="Principal not found"):
            kb.issue_token("nobody@example.com")

    def test_issue_token_round_trips_through_auth_provider(self, kb: Ontology) -> None:
        token = kb.issue_token("alice@example.com")
        principal = TokenAuthProvider(kb.backend).resolve(token)
        assert principal.id == "alice@example.com"

    def test_revoke_token_unknown_credential_raises_not_found(self, kb: Ontology) -> None:
        from ontolith.core.errors import NotFoundError

        with pytest.raises(NotFoundError, match="Token credential not found"):
            kb.revoke_token("nonexistent")

    def test_list_tokens_reflects_issuance_and_revocation(self, kb: Ontology) -> None:
        kb.issue_token("alice@example.com")
        kb.issue_token("alice@example.com")
        credentials = kb.list_tokens("alice@example.com")
        assert len(credentials) == 2
        assert all(c.revoked_at is None for c in credentials)

        kb.revoke_token(credentials[0].id)
        refreshed = kb.list_tokens("alice@example.com")
        revoked = next(c for c in refreshed if c.id == credentials[0].id)
        assert revoked.revoked_at is not None

    def test_list_tokens_empty_for_principal_with_no_credentials(self, kb: Ontology) -> None:
        assert kb.list_tokens("alice@example.com") == []
