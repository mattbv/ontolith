"""Identity and authentication port abstractions."""

from typing import Protocol

from ontolith.identity.principal import Principal


class AuthProvider(Protocol):
    """Abstract port for resolving a caller's credential to a Principal.

    AuthProviders resolve a bearer credential (e.g. an API-key token) to the
    principal it authenticates as — the principal is never taken as a
    caller-asserted ID. See ADR-0014 for the interim per-principal API-key
    model implemented by the concrete `TokenAuthProvider`; full OIDC/
    workload-identity support remains future work.
    """

    def resolve(self, token: str) -> Principal:
        """Resolve a raw bearer token to its Principal.

        Args:
            token: Raw bearer token supplied by the caller

        Returns:
            The Principal the token authenticates as

        Raises:
            AuthError: token is invalid, unknown, or revoked
        """
        ...


__all__ = ["AuthProvider"]
