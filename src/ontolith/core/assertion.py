"""Assertion value object - atomic unit of knowledge.

Per SPEC §5.3: Assertions are append-only. Only status, valid_to, and
supersedes links may be modified after creation.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Assertion(BaseModel):
    """Atomic unit of knowledge with full provenance.

    Attributes:
        id: Unique identifier (ULID)
        namespace: Namespace slug
        subject: Entity ID this assertion is about
        predicate: Qualified property/relation name (e.g., "Person.name")
        value_kind: Whether value is a literal or entity reference
        value_type: Type of literal value (if value_kind is "literal")
        value: The actual value (literal or entity ID)
        author: Principal ID who made this assertion
        acting_as: Principal ID if delegated (optional)
        source: Source of information (URI or description)
        confidence: Author's stated belief (0.0-1.0)
        rationale: Why this assertion was made
        model: Model family+version for AI authors
        asserted_at: When we learned this fact
        valid_from: When the fact became/becomes true (defaults to asserted_at)
        valid_to: When the fact stopped being true (NULL = still valid)
        status: Current lifecycle state
        proposal_id: Proposal that introduced this assertion
        supersedes: Previous assertion ID if this supersedes another
        metadata: Open JSON blob for future evolution
    """

    # Core identity
    id: str
    namespace: str
    subject: str
    predicate: str

    # Value
    value_kind: Literal["literal", "ref"]
    value_type: str | None = None  # Required when value_kind="literal"
    value: str  # Literal value or entity ID

    # Provenance
    author: str
    acting_as: str | None = None
    source: str | None = None
    confidence: float | None = Field(None, ge=0.0, le=1.0)
    rationale: str | None = None
    model: str | None = None  # Required for AI authors

    # Temporal
    asserted_at: datetime
    valid_from: datetime | None = None  # Defaults to asserted_at if not provided
    valid_to: datetime | None = None

    # Lifecycle
    status: Literal["active", "superseded", "retracted", "flagged"] = "active"
    proposal_id: str | None = None
    supersedes: str | None = None

    # Future evolution
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def set_defaults_and_validate(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Set valid_from default and validate value_type based on value_kind."""
        # Default valid_from to asserted_at (SPEC §5.3)
        if "valid_from" not in values or values["valid_from"] is None:
            values["valid_from"] = values.get("asserted_at")

        # Validate value_type based on value_kind
        value_kind = values.get("value_kind")
        value_type = values.get("value_type")

        if value_kind == "literal" and value_type is None:
            raise ValueError("value_type is required when value_kind is 'literal'")
        if value_kind == "ref" and value_type is not None:
            raise ValueError("value_type must be None when value_kind is 'ref'")

        return values

    model_config = ConfigDict(
        frozen=True,  # Immutable by default
        # Only status, valid_to, supersedes can be "mutated" via copy(update={...})
    )


__all__ = ["Assertion"]
