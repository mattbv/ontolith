"""Unit tests for the LinkML-aligned YAML front-end (SPEC §6.1, ADR-0013)."""

import pytest

from ontolith.core.errors import SchemaError
from ontolith.schema import ConceptDef, PropertyDef, RelationDef, SchemaIR
from ontolith.schema.linkml import from_yaml, to_yaml


class TestToYaml:
    def test_simple_schema_serializes(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={"name": PropertyDef(name="name", value_type="Text", required=True)},
                )
            },
        )
        text = to_yaml(schema)
        assert "id: default" in text
        assert "version: 1" in text
        assert "range: string" in text

    def test_float_serializes_as_double_not_float(self) -> None:
        """KI-068: emitting LinkML `range: float` (32-bit `xsd:float` in real
        LinkML tooling) for a value Ontolith stores as a Python float/
        IEEE-754 double was a lossy round-trip and diverged from
        schema/rdf.py's own Float mapping (`XSD.double`) - `double` is the
        precision-accurate choice, matching what's actually stored."""
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Sensor": ConceptDef(
                    name="Sensor",
                    properties={"reading": PropertyDef(name="reading", value_type="Float")},
                )
            },
        )
        text = to_yaml(schema)
        assert "range: double" in text
        assert "range: float" not in text

    def test_temporality_encoded_as_annotation(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "salary": PropertyDef(
                            name="salary", value_type="Integer", temporality="time_varying"
                        )
                    },
                )
            },
        )
        text = to_yaml(schema)
        assert "ontolith_temporality: time_varying" in text

    def test_static_temporality_omitted(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={"name": PropertyDef(name="name", value_type="Text")},
                )
            },
        )
        text = to_yaml(schema)
        assert "annotations" not in text

    def test_schema_level_metadata_serialized(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={},
            metadata={
                "prefixes": {"ex": "https://example.org/"},
                "default_prefix": "ex",
                "description": "An example schema",
            },
        )
        text = to_yaml(schema)
        assert "default_prefix: ex" in text
        assert "description: An example schema" in text

    def test_schema_level_metadata_roundtrips(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={},
            metadata={
                "prefixes": {"ex": "https://example.org/"},
                "default_prefix": "ex",
                "description": "An example schema",
            },
        )
        reconstructed = from_yaml(to_yaml(schema))
        assert reconstructed.metadata == schema.metadata

    def test_unsupported_metadata_key_raises(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={},
            metadata={"custom_key": "would be silently dropped without this guard"},
        )
        with pytest.raises(SchemaError, match="Unsupported schema metadata key"):
            to_yaml(schema)

    def test_required_relation_serialized(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Organization": ConceptDef(name="Organization", properties={}, relations={}),
                "Person": ConceptDef(
                    name="Person",
                    properties={},
                    relations={
                        "employer": RelationDef(
                            name="employer", target_concept="Organization", required=True
                        )
                    },
                ),
            },
        )
        text = to_yaml(schema)
        assert "required: true" in text

    def test_json_value_type_disambiguated_from_text(self) -> None:
        """Text and JSON both map to LinkML range: string — JSON must carry an
        explicit annotation so from_yaml doesn't collapse it back to Text."""
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Thing": ConceptDef(
                    name="Thing",
                    properties={"payload": PropertyDef(name="payload", value_type="JSON")},
                )
            },
        )
        text = to_yaml(schema)
        assert "ontolith_value_type: JSON" in text

        reconstructed = from_yaml(text)
        assert reconstructed.concepts["Thing"].properties["payload"].value_type == "JSON"


class TestFromYaml:
    def test_simple_schema_parses(self) -> None:
        text = """
        id: default
        name: default
        version: 1
        classes:
          Person:
            attributes:
              name:
                range: string
                required: true
        """
        schema = from_yaml(text)
        assert schema.namespace == "default"
        assert schema.version == 1
        assert schema.concepts["Person"].properties["name"].value_type == "Text"
        assert schema.concepts["Person"].properties["name"].required is True

    def test_double_range_parses_as_float(self) -> None:
        """KI-068: `double` is what to_yaml now emits for Float (see
        TestToYaml.test_float_serializes_as_double_not_float) - the reverse
        mapping must accept it back as Float, not treat it as unknown."""
        text = """
        id: default
        version: 1
        classes:
          Sensor:
            attributes:
              reading:
                range: double
        """
        schema = from_yaml(text)
        assert schema.concepts["Sensor"].properties["reading"].value_type == "Float"

    def test_float_range_still_parses_for_backward_compatibility(self) -> None:
        """`range: float` - the legacy emission this bridge used before
        KI-068 - must keep importing as Float: hand-authored LinkML files
        and schemas exported by an older Ontolith version both still use
        it, and Python's float parsing doesn't distinguish 32-bit from
        64-bit on read anyway."""
        text = """
        id: default
        version: 1
        classes:
          Sensor:
            attributes:
              reading:
                range: float
        """
        schema = from_yaml(text)
        assert schema.concepts["Sensor"].properties["reading"].value_type == "Float"

    def test_relation_range_matching_class_becomes_relation(self) -> None:
        text = """
        id: default
        version: 1
        classes:
          Organization:
            attributes:
              name:
                range: string
          Person:
            attributes:
              name:
                range: string
              employer:
                range: Organization
                inverse: employees
                multivalued: false
        """
        schema = from_yaml(text)
        employer = schema.concepts["Person"].relations["employer"]
        assert employer.target_concept == "Organization"
        assert employer.inverse == "employees"

    def test_multivalued_maps_to_cardinality_many(self) -> None:
        text = """
        id: default
        version: 1
        classes:
          Person:
            attributes:
              nickname:
                range: string
                multivalued: true
        """
        schema = from_yaml(text)
        assert schema.concepts["Person"].properties["nickname"].cardinality == "many"

    def test_temporality_annotation_parsed(self) -> None:
        text = """
        id: default
        version: 1
        classes:
          Person:
            attributes:
              salary:
                range: integer
                annotations:
                  ontolith_temporality: time_varying
        """
        schema = from_yaml(text)
        assert schema.concepts["Person"].properties["salary"].temporality == "time_varying"

    def test_invalid_temporality_annotation_raises(self) -> None:
        text = """
        id: default
        version: 1
        classes:
          Person:
            attributes:
              salary:
                range: integer
                annotations:
                  ontolith_temporality: sometimes
        """
        with pytest.raises(SchemaError, match="invalid ontolith_temporality"):
            from_yaml(text)

    def test_missing_namespace_raises(self) -> None:
        text = "version: 1\nclasses: {}"
        with pytest.raises(SchemaError, match="must set 'id' or 'name'"):
            from_yaml(text)

    def test_non_integer_version_raises(self) -> None:
        text = 'id: default\nversion: "1.0.3"\nclasses: {}'
        with pytest.raises(SchemaError, match="must be an integer"):
            from_yaml(text)

    def test_boolean_version_raises(self) -> None:
        """bool is an int subclass — isinstance(True, int) is True — so this
        must be excluded explicitly or `version: true` would silently parse as 1."""
        text = "id: default\nversion: true\nclasses: {}"
        with pytest.raises(SchemaError, match="must be an integer"):
            from_yaml(text)

    def test_non_mapping_document_raises(self) -> None:
        with pytest.raises(SchemaError, match="must be a mapping at the top level"):
            from_yaml("- just\n- a\n- list\n")

    def test_classes_not_a_mapping_raises(self) -> None:
        text = "id: default\nversion: 1\nclasses: [not, a, mapping]"
        with pytest.raises(SchemaError, match="'classes' must be a mapping"):
            from_yaml(text)

    @pytest.mark.parametrize("bad_value", ["0", "false", '""'])
    def test_classes_falsy_wrong_type_raises(self, bad_value: str) -> None:
        """A falsy-but-wrong-type `classes:` must still raise, not be silently
        coerced to {} the way `classes: null` (absent) legitimately is."""
        text = f"id: default\nversion: 1\nclasses: {bad_value}"
        with pytest.raises(SchemaError, match="'classes' must be a mapping"):
            from_yaml(text)

    def test_classes_null_is_treated_as_empty(self) -> None:
        text = "id: default\nversion: 1\nclasses:"
        schema = from_yaml(text)
        assert schema.concepts == {}

    def test_class_body_not_a_mapping_raises(self) -> None:
        text = "id: default\nversion: 1\nclasses:\n  Person: not-a-mapping"
        with pytest.raises(SchemaError, match="Class 'Person' must be a mapping"):
            from_yaml(text)

    def test_attributes_not_a_mapping_raises(self) -> None:
        text = "id: default\nversion: 1\nclasses:\n  Person:\n    attributes: [nope]"
        with pytest.raises(SchemaError, match="attributes must be a mapping"):
            from_yaml(text)

    @pytest.mark.parametrize("bad_value", ["0", "false", '""'])
    def test_attributes_falsy_wrong_type_raises(self, bad_value: str) -> None:
        """A falsy-but-wrong-type `attributes:` must still raise, not be silently
        coerced to {} the way `attributes: null` (absent) legitimately is."""
        text = f"id: default\nversion: 1\nclasses:\n  Person:\n    attributes: {bad_value}"
        with pytest.raises(SchemaError, match="attributes must be a mapping"):
            from_yaml(text)

    def test_attributes_null_is_treated_as_empty(self) -> None:
        text = "id: default\nversion: 1\nclasses:\n  Person:\n    attributes:"
        schema = from_yaml(text)
        assert schema.concepts["Person"].properties == {}

    def test_slot_body_not_a_mapping_raises(self) -> None:
        text = "id: default\nversion: 1\nclasses:\n  Person:\n    attributes:\n      name: not-a-mapping"
        with pytest.raises(SchemaError, match="must be a mapping"):
            from_yaml(text)

    def test_invalid_value_type_annotation_raises(self) -> None:
        text = """
        id: default
        version: 1
        classes:
          Thing:
            attributes:
              payload:
                range: string
                annotations:
                  ontolith_value_type: NotARealType
        """
        with pytest.raises(SchemaError, match="invalid ontolith_value_type"):
            from_yaml(text)

    def test_unknown_range_raises(self) -> None:
        text = """
        id: default
        version: 1
        classes:
          Person:
            attributes:
              name:
                range: NotARealClassOrType
        """
        with pytest.raises(SchemaError, match="unknown range"):
            from_yaml(text)

    def test_default_range_applied_when_slot_omits_range(self) -> None:
        text = """
        id: default
        version: 1
        default_range: integer
        classes:
          Person:
            attributes:
              age: {}
        """
        schema = from_yaml(text)
        assert schema.concepts["Person"].properties["age"].value_type == "Integer"

    def test_slot_range_overrides_default_range(self) -> None:
        text = """
        id: default
        version: 1
        default_range: integer
        classes:
          Person:
            attributes:
              name:
                range: string
        """
        schema = from_yaml(text)
        assert schema.concepts["Person"].properties["name"].value_type == "Text"

    def test_no_default_range_falls_back_to_string(self) -> None:
        """Unchanged historical behavior when default_range isn't declared."""
        text = """
        id: default
        version: 1
        classes:
          Person:
            attributes:
              name: {}
        """
        schema = from_yaml(text)
        assert schema.concepts["Person"].properties["name"].value_type == "Text"

    def test_default_range_naming_a_class_makes_unranged_slots_relations(self) -> None:
        text = """
        id: default
        version: 1
        default_range: Organization
        classes:
          Person:
            attributes:
              employer: {}
          Organization:
            attributes: {}
        """
        schema = from_yaml(text)
        assert "employer" in schema.concepts["Person"].relations
        assert schema.concepts["Person"].relations["employer"].target_concept == "Organization"

    def test_invalid_default_range_raises(self) -> None:
        text = """
        id: default
        version: 1
        default_range: NotARealClassOrType
        classes:
          Person:
            attributes:
              name: {}
        """
        with pytest.raises(SchemaError, match="'default_range'"):
            from_yaml(text)

    @pytest.mark.parametrize("key", ["slots", "imports", "types", "subsets", "enums"])
    def test_unsupported_schema_level_construct_raises(self, key: str) -> None:
        text = f"""
        id: default
        version: 1
        {key}: {{}}
        classes: {{}}
        """
        with pytest.raises(SchemaError, match="Unsupported LinkML construct at schema level"):
            from_yaml(text)

    @pytest.mark.parametrize("key", ["is_a", "mixins", "slot_usage", "tree_root", "abstract"])
    def test_unsupported_class_level_construct_raises(self, key: str) -> None:
        text = f"""
        id: default
        version: 1
        classes:
          Person:
            {key}: SomethingElse
            attributes: {{}}
        """
        with pytest.raises(SchemaError, match="Unsupported LinkML construct on class"):
            from_yaml(text)

    @pytest.mark.parametrize(
        "key,value",
        [
            ("pattern", "^[A-Z]+$"),
            ("any_of", [{"range": "string"}]),
            ("all_of", [{"range": "string"}]),
            ("exactly_one_of", [{"range": "string"}]),
            ("none_of", [{"range": "string"}]),
            ("permissible_values", {"a": {}}),
            ("identifier", True),
            ("key", True),
            ("alias", "person_name"),
            ("ifabsent", "string(unknown)"),
            ("readonly", "true"),
            ("recommended", True),
        ],
    )
    def test_unsupported_slot_level_construct_raises(self, key: str, value: object) -> None:
        text = {
            "id": "default",
            "version": 1,
            "classes": {"Person": {"attributes": {"name": {"range": "string", key: value}}}},
        }
        import yaml as _yaml

        with pytest.raises(SchemaError, match="Unsupported LinkML construct on"):
            from_yaml(_yaml.safe_dump(text))


class TestRoundTrip:
    def test_to_yaml_then_from_yaml_preserves_ir(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Organization": ConceptDef(name="Organization", properties={}, relations={}),
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text", required=True),
                        "salary": PropertyDef(
                            name="salary",
                            value_type="Integer",
                            temporality="time_varying",
                            description="Historical salary",
                        ),
                    },
                    relations={
                        "employer": RelationDef(
                            name="employer",
                            target_concept="Organization",
                            inverse="employees",
                            temporality="time_varying",
                        )
                    },
                ),
            },
        )

        reconstructed = from_yaml(to_yaml(schema))
        assert reconstructed.concepts == schema.concepts
        assert reconstructed.namespace == schema.namespace
        assert reconstructed.version == schema.version


class TestCrossBridgeConsistency:
    """KI-068: the LinkML and RDF/OWL bridges must agree on Float's
    precision. Pinned here, one level more specific than each bridge's own
    per-module test coverage (TestToYaml.test_float_serializes_as_double_
    not_float above; test_schema_rdf.py's own Float assertion) - those two
    only prove each bridge is internally consistent with itself. A future
    change to either mapping that reintroduces the divergence this KI
    fixed would otherwise surface as two independently-passing tests that
    happen to disagree with each other, with nothing pointing at the
    actual invariant broken."""

    def test_float_maps_to_the_same_precision_in_both_bridges(self) -> None:
        from rdflib.namespace import XSD

        from ontolith.schema.linkml import _VALUE_TYPE_TO_LINKML_RANGE
        from ontolith.schema.rdf import _VALUE_TYPE_TO_XSD

        assert _VALUE_TYPE_TO_LINKML_RANGE["Float"] == "double"
        assert _VALUE_TYPE_TO_XSD["Float"] == XSD.double
