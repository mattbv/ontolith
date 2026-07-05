"""Unit tests for the class-based schema DSL (SPEC §6.2)."""

import pytest

from ontolith.core.errors import SchemaError
from ontolith.schema import (
    Concept,
    Date,
    Integer,
    Property,
    Ref,
    Relation,
    Text,
    compile_schema,
)


class TestBasicCompilation:
    def test_simple_concept_compiles_to_property_defs(self) -> None:
        class Person(Concept):
            name: Text
            born: Date | None = None

        schema = compile_schema("default", 1, Person)

        person = schema.concepts["Person"]
        assert person.properties["name"].value_type == "Text"
        assert person.properties["name"].required is True
        assert person.properties["born"].value_type == "Date"
        assert person.properties["born"].required is False

    def test_default_temporality_is_static(self) -> None:
        class Person(Concept):
            name: Text

        schema = compile_schema("default", 1, Person)
        assert schema.concepts["Person"].properties["name"].temporality == "static"

    def test_property_spec_sets_temporality_and_cardinality(self) -> None:
        class Person(Concept):
            name: Text
            nickname: Text | None = Property(cardinality="many", temporality="time_varying")

        schema = compile_schema("default", 1, Person)
        nickname = schema.concepts["Person"].properties["nickname"]
        assert nickname.cardinality == "many"
        assert nickname.temporality == "time_varying"
        assert nickname.required is False

    def test_property_spec_infers_required_from_bare_annotation(self) -> None:
        """A bare (non-Optional) annotation stays required even when a
        Property(...) spec is attached for unrelated reasons (temporality here) —
        attaching a spec must not silently flip required to False."""

        class Person(Concept):
            name: Text
            salary: Integer = Property(temporality="time_varying")

        schema = compile_schema("default", 1, Person)
        assert schema.concepts["Person"].properties["salary"].required is True

    def test_property_spec_explicit_required_overrides_annotation(self) -> None:
        class Person(Concept):
            name: Text
            nickname: Text = Property(required=False, cardinality="many")

        schema = compile_schema("default", 1, Person)
        assert schema.concepts["Person"].properties["nickname"].required is False


class TestRelations:
    def test_relation_with_inverse_and_temporality(self) -> None:
        class Organization(Concept):
            name: Text

        class Person(Concept):
            name: Text
            employer: Ref["Organization"] = Relation(
                inverse="employees", temporality="time_varying"
            )

        schema = compile_schema("default", 1, Person, Organization)

        employer = schema.concepts["Person"].relations["employer"]
        assert employer.target_concept == "Organization"
        assert employer.inverse == "employees"
        assert employer.temporality == "time_varying"

    def test_mutually_referencing_concepts_compile(self) -> None:
        """Ref[...] captures a string, so forward-declared mutual references work."""

        class Person(Concept):
            name: Text
            employer: Ref["Organization"] = Relation(inverse="employees")

        class Organization(Concept):
            name: Text
            employees: Ref["Person"] = Relation(inverse="employer", cardinality="many")

        schema = compile_schema("default", 1, Person, Organization)
        assert schema.concepts["Person"].relations["employer"].target_concept == "Organization"
        assert schema.concepts["Organization"].relations["employees"].target_concept == "Person"

    def test_plain_ref_without_relation_spec_defaults_required_true(self) -> None:
        class Organization(Concept):
            name: Text

        class Person(Concept):
            name: Text
            employer: Ref["Organization"]

        schema = compile_schema("default", 1, Person, Organization)
        assert schema.concepts["Person"].relations["employer"].required is True

    def test_optional_ref_defaults_required_false(self) -> None:
        class Organization(Concept):
            name: Text

        class Person(Concept):
            name: Text
            employer: Ref["Organization"] | None = None

        schema = compile_schema("default", 1, Person, Organization)
        assert schema.concepts["Person"].relations["employer"].required is False

    def test_relation_spec_infers_required_from_bare_annotation(self) -> None:
        """A bare (non-Optional) Ref[...] stays required even when a
        Relation(...) spec is attached for unrelated reasons (temporality here)."""

        class Organization(Concept):
            name: Text

        class Person(Concept):
            name: Text
            employer: Ref["Organization"] = Relation(temporality="time_varying")

        schema = compile_schema("default", 1, Person, Organization)
        assert schema.concepts["Person"].relations["employer"].required is True

    def test_missing_relation_target_raises_schema_error(self) -> None:
        class Person(Concept):
            name: Text
            employer: Ref["Organization"] = Relation()  # noqa: F821 -- deliberately unresolved

        with pytest.raises(SchemaError, match="unknown concept"):
            compile_schema("default", 1, Person)


class TestCompileSchemaEntrypoint:
    def test_compile_schema_is_explicit_not_global_registry(self) -> None:
        """Defining a Concept subclass doesn't implicitly register it anywhere."""

        class Widget(Concept):
            name: Text

        schema = compile_schema("default", 1)
        assert "Widget" not in schema.concepts

    def test_non_concept_class_raises(self) -> None:
        class NotACompiledConcept:
            pass

        with pytest.raises(SchemaError, match="not a compiled Concept subclass"):
            compile_schema("default", 1, NotACompiledConcept)  # type: ignore[arg-type]

    def test_bad_annotation_raises_schema_error(self) -> None:
        with pytest.raises(SchemaError, match="scalar types"):

            class Broken(Concept):
                name: str  # not one of the ontolith scalar markers

    def test_ref_with_non_string_target_raises_schema_error(self) -> None:
        with pytest.raises(SchemaError, match="target must be a string"):
            Ref[Text]  # type: ignore[index]
