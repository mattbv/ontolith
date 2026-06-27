"""Entity value object - concrete individual of a concept.

Per SPEC §5.2: Entities carry no attribute values directly - all attributes
are Assertions about the entity.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class Entity(BaseModel):
    """Concrete individual of a concept.

    Attributes:
        id: Unique identifier (ULID)
        namespace: Namespace slug
        concept: Concept name (e.g., "Person", "Organization")
        natural_key: Optional unique key within (namespace, concept)
        created_at: When this entity was created
        created_by: Principal ID who created this entity
    """

    id: str
    namespace: str
    concept: str
    natural_key: str | None = None
    created_at: datetime
    created_by: str

    model_config = ConfigDict(frozen=True)  # Immutable


__all__ = ["Entity"]
