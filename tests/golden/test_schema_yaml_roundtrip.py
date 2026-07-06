"""Golden test: IR -> YAML -> IR idempotence (Implementation Plan §6).

Exercises `ontolith.schema.linkml.to_yaml`/`from_yaml` across representative
schemas covering the full range of PropertyDef/RelationDef field combinations
the v1 LinkML dialect (ADR-0013) supports.
"""

from ontolith.schema import ConceptDef, PropertyDef, RelationDef, SchemaIR
from ontolith.schema.linkml import from_yaml, to_yaml


def _assert_roundtrips(schema: SchemaIR) -> None:
    reconstructed = from_yaml(to_yaml(schema))
    assert reconstructed.namespace == schema.namespace
    assert reconstructed.version == schema.version
    assert reconstructed.concepts == schema.concepts
    # Idempotent: doing it again produces the same YAML text.
    assert to_yaml(schema) == to_yaml(reconstructed)


class TestSchemaYamlRoundtrip:
    def test_single_concept_single_property(self) -> None:
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
        _assert_roundtrips(schema)

    def test_relation_with_inverse(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Organization": ConceptDef(name="Organization", properties={}, relations={}),
                "Person": ConceptDef(
                    name="Person",
                    properties={"name": PropertyDef(name="name", value_type="Text")},
                    relations={
                        "employer": RelationDef(
                            name="employer",
                            target_concept="Organization",
                            inverse="employees",
                        )
                    },
                ),
            },
        )
        _assert_roundtrips(schema)

    def test_time_varying_relation(self) -> None:
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
                            name="employer",
                            target_concept="Organization",
                            temporality="time_varying",
                        )
                    },
                ),
            },
        )
        _assert_roundtrips(schema)

    def test_multivalued_property_and_relation(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Organization": ConceptDef(name="Organization", properties={}, relations={}),
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "nickname": PropertyDef(
                            name="nickname", value_type="Text", cardinality="many"
                        )
                    },
                    relations={
                        "employer": RelationDef(
                            name="employer",
                            target_concept="Organization",
                            cardinality="many",
                        )
                    },
                ),
            },
        )
        _assert_roundtrips(schema)

    def test_all_scalar_value_types(self) -> None:
        properties = {
            "a": PropertyDef(name="a", value_type="Text"),
            "b": PropertyDef(name="b", value_type="Integer"),
            "c": PropertyDef(name="c", value_type="Float"),
            "d": PropertyDef(name="d", value_type="Boolean"),
            "e": PropertyDef(name="e", value_type="Date"),
            "f": PropertyDef(name="f", value_type="DateTime"),
            "g": PropertyDef(name="g", value_type="URI"),
            "h": PropertyDef(name="h", value_type="JSON"),
        }
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={"Thing": ConceptDef(name="Thing", properties=properties)},
        )
        _assert_roundtrips(schema)

    def test_descriptions_on_concept_property_and_relation(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Organization": ConceptDef(name="Organization", properties={}, relations={}),
                "Person": ConceptDef(
                    name="Person",
                    description="A human being",
                    properties={
                        "name": PropertyDef(
                            name="name", value_type="Text", description="Full legal name"
                        )
                    },
                    relations={
                        "employer": RelationDef(
                            name="employer",
                            target_concept="Organization",
                            description="Current employer",
                        )
                    },
                ),
            },
        )
        _assert_roundtrips(schema)
