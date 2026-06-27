"""Contradiction model - unresolved conflicts between static assertions.

Per SPEC §10.3: when sources disagree on a static fact, a Contradiction object
groups the conflicting assertions for human review rather than silently overwriting.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Contradiction(BaseModel):
    """Open or resolved conflict between static assertions.

    Attributes:
        id: Unique contradiction ID (ULID)
        namespace: Namespace this contradiction belongs to
        subject: Entity the conflicting assertions are about
        predicate: The predicate where sources disagree
        state: Whether the contradiction is open or resolved
        member_ids: IDs of the flagged assertions in this contradiction
        created_at: When this contradiction was first detected
        resolved_by: Principal ID who resolved it (if resolved)
        resolved_at: When it was resolved (if resolved)
        metadata: Open JSON blob for future extension
    """

    id: str
    namespace: str
    subject: str
    predicate: str
    state: Literal["open", "resolved"] = "open"
    member_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)


__all__ = ["Contradiction"]
