"""PrincipalCredential - hashed API-key token bound to a Principal (ADR-0014).

The raw token is generated once at issuance and never persisted; only its
SHA-256 hash is stored. Resolution (token -> Principal) happens via
AuthProvider/TokenAuthProvider, never by trusting a caller-supplied ID.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PrincipalCredential(BaseModel):
    """A single issued API-key credential for a principal.

    Attributes:
        id: Unique identifier (ULID) — a stable handle for revocation,
            independent of the (never-stored) raw token
        principal_id: Principal this credential authenticates as
        token_hash: SHA-256 hex digest of the raw token
        created_at: When this credential was issued
        revoked_at: When this credential was revoked, if it has been
    """

    id: str
    principal_id: str
    token_hash: str
    created_at: datetime
    revoked_at: datetime | None = None

    model_config = ConfigDict(frozen=True)


__all__ = ["PrincipalCredential"]
