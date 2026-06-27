"""Identity and authentication port abstractions."""

from typing import Protocol


class AuthProvider(Protocol):
    """Abstract port for authentication providers.

    AuthProviders resolve requests to principal identities.
    Implementations include OIDC, workload identity, API keys, etc.

    This is a stub for M0. Full interface will be defined in M1 based on
    SPEC §8.2 requirements.
    """

    # Full interface in M1:
    # def authenticate(self, request: Request) -> Identity: ...
    # Identity includes: principal_id, acting_as (optional)


__all__ = ["AuthProvider"]
