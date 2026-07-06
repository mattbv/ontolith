"""Unit tests for class-stub codegen (SPEC §6.1: IR -> classes).

Covers the `classes -> IR -> class stubs -> IR` half of the required
bidirectional-codegen golden idempotence test.
"""

from ontolith.schema import (
    Concept,
    Date,
    Integer,
    Property,
    Ref,
    Relation,
    Text,
    compile_schema,
    generate_class_stubs,
)


def _roundtrip_through_stubs(schema):
    source = generate_class_stubs(schema)
    module_globals: dict = {}
    exec(compile(source, "<generated>", "exec"), module_globals)  # noqa: S102
    concept_classes = [
        obj
        for obj in module_globals.values()
        if isinstance(obj, type) and issubclass(obj, Concept) and obj is not Concept
    ]
    return compile_schema(schema.namespace, schema.version, *concept_classes)


class TestGenerateClassStubs:
    def test_output_is_deterministic(self) -> None:
        class Person(Concept):
            name: Text

        schema = compile_schema("default", 1, Person)
        assert generate_class_stubs(schema) == generate_class_stubs(schema)

    def test_simple_concept_roundtrips(self) -> None:
        class Person(Concept):
            name: Text
            born: Date | None = None

        schema = compile_schema("default", 1, Person)
        reconstructed = _roundtrip_through_stubs(schema)

        assert reconstructed.concepts.keys() == schema.concepts.keys()
        assert reconstructed.concepts["Person"].properties == schema.concepts["Person"].properties

    def test_relation_with_inverse_and_temporality_roundtrips(self) -> None:
        class Organization(Concept):
            name: Text

        class Person(Concept):
            name: Text
            employer: Ref["Organization"] = Relation(
                inverse="employees", temporality="time_varying"
            )

        schema = compile_schema("default", 1, Person, Organization)
        reconstructed = _roundtrip_through_stubs(schema)

        assert (
            reconstructed.concepts["Person"].relations["employer"]
            == schema.concepts["Person"].relations["employer"]
        )

    def test_mutually_referencing_concepts_roundtrip(self) -> None:
        class Person(Concept):
            name: Text
            employer: Ref["Organization"] = Relation(inverse="employees")

        class Organization(Concept):
            name: Text
            employees: Ref["Person"] = Relation(inverse="employer", cardinality="many")

        schema = compile_schema("default", 1, Person, Organization)
        reconstructed = _roundtrip_through_stubs(schema)

        assert reconstructed.concepts == schema.concepts

    def test_property_with_cardinality_temporality_description_roundtrips(self) -> None:
        class Person(Concept):
            name: Text
            salary: Integer = Property(
                cardinality="many",
                temporality="time_varying",
                description="Historical salary figures",
                required=True,
            )

        schema = compile_schema("default", 1, Person)
        reconstructed = _roundtrip_through_stubs(schema)

        assert reconstructed.concepts["Person"].properties == schema.concepts["Person"].properties

    def test_optional_property_with_overrides_roundtrips(self) -> None:
        class Person(Concept):
            name: Text
            nickname: Text | None = Property(cardinality="many", description="Known aliases")

        schema = compile_schema("default", 1, Person)
        reconstructed = _roundtrip_through_stubs(schema)

        nickname = reconstructed.concepts["Person"].properties["nickname"]
        assert nickname == schema.concepts["Person"].properties["nickname"]
        assert nickname.required is False

    def test_relation_with_cardinality_and_description_roundtrips(self) -> None:
        class Organization(Concept):
            name: Text
            employees: Ref["Person"] = Relation(
                cardinality="many", description="People employed here"
            )

        class Person(Concept):
            name: Text

        schema = compile_schema("default", 1, Organization, Person)
        reconstructed = _roundtrip_through_stubs(schema)

        assert (
            reconstructed.concepts["Organization"].relations["employees"]
            == schema.concepts["Organization"].relations["employees"]
        )

    def test_concept_description_with_quotes_roundtrips(self) -> None:
        """A description containing quote characters must not break the
        generated source (regression: repr(), not a raw triple-quoted string)."""

        class Person(Concept):
            '''He said """hello""" and it\'s "fine".'''

            name: Text

        schema = compile_schema("default", 1, Person)
        source = generate_class_stubs(schema)
        compile(source, "<generated>", "exec")  # raises SyntaxError if malformed

        reconstructed = _roundtrip_through_stubs(schema)
        assert reconstructed.concepts["Person"].description == schema.concepts["Person"].description

    def test_generated_source_is_valid_python_importing_public_names(self) -> None:
        class Person(Concept):
            name: Text

        schema = compile_schema("default", 1, Person)
        source = generate_class_stubs(schema)

        assert "from ontolith import Concept, Property, Ref, Relation" in source
        compile(source, "<generated>", "exec")  # raises SyntaxError if malformed
