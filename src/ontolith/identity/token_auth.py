"""Token-based AuthProvider — per-principal API-key authentication (ADR-0014).

Not re-exported from `ontolith.identity`'s package `__init__`: `store.base`
already imports `Principal` from `ontolith.identity`, so eagerly importing
this module (which depends on `StorageBackend`) from `identity/__init__.py`
would create a circular import. Import it directly:

    from ontolith.identity.token_auth import TokenAuthProvider, hash_token
"""

import hashlib

from ontolith.core.errors import AuthError
from ontolith.identity.principal import Principal
from ontolith.store.base import StorageBackend


def hash_token(raw_token: str) -> str:
    """Hash a raw bearer token for storage/lookup.

    SHA-256, not a slow password hash (bcrypt/scrypt/argon2): the input is
    already a high-entropy random secret (`secrets.token_urlsafe(32)`), not a
    low-entropy human password, so brute-force resistance from a slow hash
    buys nothing here — it would only add latency to every authenticated call.
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()


class TokenAuthProvider:
    """AuthProvider that resolves API-key tokens via the abstract StorageBackend port.

    Depends only on StorageBackend (the port), the same pattern Ontology
    itself uses (`backend: StorageBackend`) — keeps identity/ from importing
    a concrete adapter, satisfying the dependency rule.
    """

    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    def resolve(self, token: str) -> Principal:
        """Resolve a raw bearer token to its Principal.

        Raises:
            AuthError: token is invalid, unknown, or revoked
        """
        principal = self._backend.get_principal_by_token_hash(hash_token(token))
        if principal is None:
            raise AuthError("Invalid or revoked token")
        return principal


__all__ = ["TokenAuthProvider", "hash_token"]
