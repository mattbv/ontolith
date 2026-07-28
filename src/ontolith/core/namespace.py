"""Namespace value object - a registered scoping boundary for an ontology.

Per SPEC §5/§12.2: all records are namespaced; the namespace registry
tracks the set of namespaces that exist, independent of whether any entity
has been written to one yet.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Namespace(BaseModel):
    """A registered namespace.

    Attributes:
        id: Namespace slug
        created_at: When this namespace was registered
        metadata: Open JSON blob for future evolution
    """

    id: str
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)  # Immutable


__all__ = ["Namespace"]
