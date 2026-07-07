"""Internal Representation (IR) for schema definitions.

The IR is a JSON-serializable format that serves as the single source of truth
for schema definitions. Multiple front-ends (class DSL, YAML) compile to IR.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class PropertyDef(BaseModel):
    """Property definition in the IR.

    Attributes:
        name: Property name
        value_type: Type of value (Text, Integer, Date, etc.)
        cardinality: Single value or many (SPEC §6)
        required: Whether the property is required
        temporality: How conflicts are handled (static vs time_varying)
        description: Optional description
    """

    name: str
    value_type: Literal["Text", "Integer", "Float", "Boolean", "Date", "DateTime", "URI", "JSON"]
    cardinality: Literal["single", "many"] = "single"
    required: bool = False
    temporality: Literal["static", "time_varying"] = "static"
    description: str | None = None

    model_config = {"frozen": True}


class RelationDef(BaseModel):
    """Relation definition in the IR.

    Attributes:
        name: Relation name
        target_concept: Target concept name
        cardinality: Single reference or many (SPEC §6)
        required: Whether the relation is required
        temporality: How conflicts are handled
        inverse: Inverse relation name (optional)
        description: Optional description
    """

    name: str
    target_concept: str
    cardinality: Literal["single", "many"] = "single"
    required: bool = False
    temporality: Literal["static", "time_varying"] = "static"
    inverse: str | None = None
    description: str | None = None

    model_config = {"frozen": True}


class ConceptDef(BaseModel):
    """Concept definition in the IR.

    Attributes:
        name: Concept name (e.g., "Person", "Organization")
        properties: Property definitions
        relations: Relation definitions
        description: Optional description
    """

    name: str
    properties: dict[str, PropertyDef] = Field(default_factory=dict)
    relations: dict[str, RelationDef] = Field(default_factory=dict)
    description: str | None = None

    model_config = {"frozen": True}


class SchemaIR(BaseModel):
    """Complete schema definition in Internal Representation.

    This is the canonical format for schema. All DSLs compile to this.

    Attributes:
        namespace: Namespace this schema belongs to
        version: Schema version (monotonically increasing)
        concepts: Concept definitions by name
        metadata: Optional metadata
    """

    namespace: str
    version: int
    concepts: dict[str, ConceptDef] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_relation_targets(self) -> "SchemaIR":
        """Validate that all relation targets reference existing concepts."""
        for concept in self.concepts.values():
            for relation in concept.relations.values():
                if relation.target_concept not in self.concepts:
                    from ontolith.core.errors import SchemaError

                    raise SchemaError(
                        f"Relation {concept.name}.{relation.name} references "
                        f"unknown concept: {relation.target_concept}"
                    )
        return self

    model_config = {"frozen": True}

    def temporality_of(self, predicate: str) -> Literal["static", "time_varying"]:
        """Resolve the declared temporality of a predicate (SPEC §10.1).

        ``predicate`` is a flat ``"Concept.field"`` string. Falls back to
        ``"static"`` (SPEC's stated default) when the concept or field isn't
        declared in this schema — e.g. a schema-less namespace, or a field
        not yet added to the active schema version.

        Args:
            predicate: Dotted predicate, e.g. "Person.name" or "Person.employer"

        Returns:
            The property's or relation's declared temporality, or "static"
            if unresolvable.
        """
        concept_name, _, field_name = predicate.partition(".")
        if not field_name:
            return "static"
        concept = self.concepts.get(concept_name)
        if concept is None:
            return "static"
        prop = concept.properties.get(field_name)
        if prop is not None:
            return prop.temporality
        relation = concept.relations.get(field_name)
        if relation is not None:
            return relation.temporality
        return "static"

    def to_json(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return self.model_dump()

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SchemaIR":
        """Deserialize from JSON-compatible dict."""
        return cls.model_validate(data)


__all__ = ["PropertyDef", "RelationDef", "ConceptDef", "SchemaIR"]
