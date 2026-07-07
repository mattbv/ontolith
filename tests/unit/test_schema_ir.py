"""Unit tests for Schema IR."""

import pytest

from ontolith.schema import ConceptDef, PropertyDef, RelationDef, SchemaIR


class TestSchemaIR:
    """Tests for Schema Internal Representation."""

    def test_create_simple_schema(self) -> None:
        """Schema can be created with concepts."""
        person = ConceptDef(
            name="Person",
            properties={
                "name": PropertyDef(
                    name="name",
                    value_type="Text",
                    required=True,
                ),
                "born": PropertyDef(
                    name="born",
                    value_type="Date",
                    required=False,
                ),
            },
        )

        schema = SchemaIR(
            namespace="test-ns",
            version=1,
            concepts={"Person": person},
        )

        assert schema.namespace == "test-ns"
        assert schema.version == 1
        assert "Person" in schema.concepts
        assert schema.concepts["Person"].name == "Person"

    def test_schema_roundtrip_json(self) -> None:
        """Schema can serialize to/from JSON."""
        schema = SchemaIR(
            namespace="test-ns",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text"),
                    },
                ),
            },
        )

        # Serialize
        json_data = schema.to_json()
        assert json_data["namespace"] == "test-ns"
        assert json_data["version"] == 1

        # Deserialize
        restored = SchemaIR.from_json(json_data)
        assert restored.namespace == schema.namespace
        assert restored.version == schema.version
        assert "Person" in restored.concepts

    def test_property_defaults(self) -> None:
        """Properties have correct defaults."""
        prop = PropertyDef(name="age", value_type="Integer")

        assert prop.required is False
        assert prop.temporality == "static"
        assert prop.description is None

    def test_relation_with_inverse(self) -> None:
        """Relations can have inverse."""
        relation = RelationDef(
            name="employer",
            target_concept="Organization",
            inverse="employees",
            temporality="time_varying",
        )

        assert relation.name == "employer"
        assert relation.target_concept == "Organization"
        assert relation.inverse == "employees"
        assert relation.temporality == "time_varying"

    def test_schema_immutable(self) -> None:
        """Schema IR objects are immutable."""
        from pydantic import ValidationError

        schema = SchemaIR(namespace="test", version=1)

        with pytest.raises(ValidationError):
            schema.version = 2  # type: ignore

    def test_relation_to_unknown_concept_raises(self) -> None:
        """Relations to nonexistent concepts raise SchemaError."""
        from ontolith.core.errors import SchemaError

        person = ConceptDef(
            name="Person",
            relations={
                "employer": RelationDef(
                    name="employer",
                    target_concept="Organization",  # Doesn't exist!
                ),
            },
        )

        with pytest.raises(SchemaError, match="unknown concept"):
            SchemaIR(
                namespace="test",
                version=1,
                concepts={"Person": person},
            )

    def test_cardinality_defaults(self) -> None:
        """Cardinality defaults to single."""
        prop = PropertyDef(name="name", value_type="Text")
        assert prop.cardinality == "single"

        rel = RelationDef(name="employer", target_concept="Organization")
        assert rel.cardinality == "single"


class TestTemporalityOf:
    """SchemaIR.temporality_of() — schema-derived conflict routing (SPEC §10.1)."""

    def _schema(self) -> SchemaIR:
        return SchemaIR(
            namespace="test",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text"),
                        "employer_name": PropertyDef(
                            name="employer_name", value_type="Text", temporality="time_varying"
                        ),
                    },
                    relations={
                        "employer": RelationDef(
                            name="employer",
                            target_concept="Organization",
                            temporality="time_varying",
                        ),
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )

    def test_resolves_static_property(self) -> None:
        assert self._schema().temporality_of("Person.name") == "static"

    def test_resolves_time_varying_property(self) -> None:
        assert self._schema().temporality_of("Person.employer_name") == "time_varying"

    def test_resolves_time_varying_relation(self) -> None:
        assert self._schema().temporality_of("Person.employer") == "time_varying"

    def test_malformed_predicate_falls_back_to_static(self) -> None:
        """No '.' separator — falls back to static rather than raising."""
        assert self._schema().temporality_of("NoDotHere") == "static"

    def test_unknown_concept_falls_back_to_static(self) -> None:
        assert self._schema().temporality_of("Vehicle.make") == "static"

    def test_unknown_field_on_known_concept_falls_back_to_static(self) -> None:
        assert self._schema().temporality_of("Person.unknown_field") == "static"
