"""Principal model - identified actors (humans, AI agents, services).

Per SPEC §8: Principals are authenticated actors who can create assertions.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Principal(BaseModel):
    """Identified actor in the system.

    Attributes:
        id: Email (human) or slug (ai/service)
        kind: Type of principal
        owner: Required for AI principals - accountable human/team
        auth_method: How this principal authenticates
        default_capability: Default permission level
        trust_level: Base trust score
        created_at: When this principal was created
        metadata: Open JSON blob for future extension
    """

    # Identity
    id: str
    kind: Literal["human", "ai", "service"]
    owner: str | None = None  # Required for AI principals

    # Authorization
    auth_method: Literal["oidc", "workload", "apikey"]
    default_capability: Literal["read", "propose", "write", "review", "admin"] = "propose"
    trust_level: int = Field(default=0, ge=0, le=10)

    # Metadata
    created_at: datetime
    metadata: dict[str, Any] = {}

    @model_validator(mode="after")
    def ai_must_have_owner(self) -> "Principal":
        """AI principals must declare an accountable owner (SPEC §8.1, ADR-0003)."""
        if self.kind == "ai" and self.owner is None:
            raise ValueError("AI principals must have an owner (SPEC §8.1)")
        return self

    model_config = ConfigDict(frozen=True)  # Immutable


__all__ = ["Principal"]
