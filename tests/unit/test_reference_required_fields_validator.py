"""Unit tests for the RequiredFieldsValidator reference plugin (KI-010, ADR-0015)."""

import tempfile
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.plugins.reference.required_fields_validator import RequiredFieldsValidator
from ontolith.plugins.views import ReadOnlyView, WriteView

PLUGIN_PRINCIPAL = "required-fields-validator"


@pytest.fixture
def kb() -> Ontology:
    with tempfile.TemporaryDirectory() as tmpdir:
        kb = Ontology.connect(Path(tmpdir) / "test.db")
        # write capability is fixture-setup convenience only - see
        # test_reference_json_exporter.py's identical note. A real validator
        # plugin only ever receives ReadOnlyView (read-only kind, ADR-0015).
        kb.create_principal(
            PLUGIN_PRINCIPAL, kind="service", auth_method="workload", default_capability="write"
        )
        yield kb
        kb.close()


class TestDefaultConfig:
    def test_person_missing_name_is_flagged(self, kb: Ontology) -> None:
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        entity = write_view.create_entity("Person", natural_key="ada")
        write_view.propose(entity.id, "Person.email", "ada@example.com", "Text")
        [assertion] = write_view.assertions(predicate="Person.email")

        violations = RequiredFieldsValidator().validate(
            assertion, ReadOnlyView(kb, PLUGIN_PRINCIPAL)
        )
        assert violations == ["Person 'ada' missing required predicate 'name'"]

    def test_person_with_name_is_not_flagged(self, kb: Ontology) -> None:
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        entity = write_view.create_entity("Person", natural_key="ada")
        write_view.propose(entity.id, "Person.name", "Ada Lovelace", "Text")
        [assertion] = write_view.assertions(predicate="Person.name")

        violations = RequiredFieldsValidator().validate(
            assertion, ReadOnlyView(kb, PLUGIN_PRINCIPAL)
        )
        assert violations == []

    def test_concept_with_no_configured_requirement_is_not_flagged(self, kb: Ontology) -> None:
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        entity = write_view.create_entity("Organization", natural_key="acme")
        write_view.propose(entity.id, "Organization.name", "Acme Corp", "Text")
        [assertion] = write_view.assertions(predicate="Organization.name")

        violations = RequiredFieldsValidator().validate(
            assertion, ReadOnlyView(kb, PLUGIN_PRINCIPAL)
        )
        assert violations == []


class TestCustomConfig:
    def test_custom_required_predicates_are_honored(self, kb: Ontology) -> None:
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        entity = write_view.create_entity("Organization", natural_key="acme")
        write_view.propose(entity.id, "Organization.name", "Acme Corp", "Text")
        [assertion] = write_view.assertions(predicate="Organization.name")

        validator = RequiredFieldsValidator({"Organization": ("name", "founded")})
        violations = validator.validate(assertion, ReadOnlyView(kb, PLUGIN_PRINCIPAL))
        assert violations == ["Organization 'acme' missing required predicate 'founded'"]

    def test_multiple_missing_predicates_all_reported(self, kb: Ontology) -> None:
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        entity = write_view.create_entity("Person", natural_key="ada")
        write_view.propose(entity.id, "Person.email", "ada@example.com", "Text")
        [assertion] = write_view.assertions(predicate="Person.email")

        validator = RequiredFieldsValidator({"Person": ("name", "email", "birthdate")})
        violations = validator.validate(assertion, ReadOnlyView(kb, PLUGIN_PRINCIPAL))
        assert len(violations) == 2
        assert "'name'" in violations[0] or "'name'" in violations[1]
        assert "'birthdate'" in violations[0] or "'birthdate'" in violations[1]


class TestNoEntityRecord:
    def test_subject_with_no_entity_record_is_flagged(self, kb: Ontology) -> None:
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        entity = write_view.create_entity("Person", natural_key="ada")
        write_view.propose(entity.id, "Person.name", "Ada Lovelace", "Text")
        [assertion] = write_view.assertions(predicate="Person.name")

        orphaned = assertion.model_copy(update={"subject": "nonexistent-entity-id"})
        violations = RequiredFieldsValidator().validate(
            orphaned, ReadOnlyView(kb, PLUGIN_PRINCIPAL)
        )
        assert violations == ["subject 'nonexistent-entity-id' has no entity record"]
