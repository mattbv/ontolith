"""Unit tests for the CsvImporter reference plugin (KI-010, ADR-0015)."""

import tempfile
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core.errors import ValidationError
from ontolith.plugins.reference.csv_importer import CsvImporter, ImportReport
from ontolith.plugins.views import WriteView

PLUGIN_PRINCIPAL = "csv-importer"


@pytest.fixture
def kb() -> Ontology:
    with tempfile.TemporaryDirectory() as tmpdir:
        kb = Ontology.connect(Path(tmpdir) / "test.db")
        # write capability so propose()/propose_ref() auto-accept (ThresholdPolicy
        # requires service+write, or service+propose with trust_level>=5) - the
        # importer's real registry-granted capability is negotiated separately
        # (PluginRegistry defaults new registrations to "propose"; an operator
        # can grant "write" at registration time for a trusted importer).
        kb.create_principal(
            PLUGIN_PRINCIPAL, kind="service", auth_method="workload", default_capability="write"
        )
        yield kb
        kb.close()


@pytest.fixture
def view(kb: Ontology) -> WriteView:
    return WriteView(kb, PLUGIN_PRINCIPAL)


class TestImportFromRows:
    def test_dedups_entity_per_natural_key(self, view: WriteView) -> None:
        rows = [
            {
                "concept": "Person",
                "natural_key": "ada",
                "predicate": "Person.name",
                "value": "Ada Lovelace",
                "value_kind": "literal",
            },
            {
                "concept": "Person",
                "natural_key": "ada",
                "predicate": "Person.title",
                "value": "Mathematician",
                "value_kind": "literal",
            },
        ]
        report = CsvImporter().import_(rows, view)
        assert isinstance(report, ImportReport)
        assert report.entities_created == 1
        assert report.assertions_proposed == 2
        assert report.rows_processed == 2

    def test_literal_row_proposes_with_default_value_type(self, view: WriteView) -> None:
        rows = [
            {
                "concept": "Person",
                "natural_key": "ada",
                "predicate": "Person.name",
                "value": "Ada Lovelace",
                "value_kind": "literal",
            }
        ]
        CsvImporter().import_(rows, view)
        [assertion] = view.assertions(predicate="Person.name")
        assert assertion.value == "Ada Lovelace"
        assert assertion.value_type == "Text"
        assert assertion.author == PLUGIN_PRINCIPAL

    def test_literal_row_honors_explicit_value_type_and_confidence(self, view: WriteView) -> None:
        rows = [
            {
                "concept": "Person",
                "natural_key": "ada",
                "predicate": "Person.age",
                "value": "36",
                "value_kind": "literal",
                "value_type": "Number",
                "confidence": "0.9",
            }
        ]
        CsvImporter().import_(rows, view)
        [assertion] = view.assertions(predicate="Person.age")
        assert assertion.value_type == "Number"
        assert assertion.confidence == 0.9

    def test_ref_row_resolves_target_natural_key(self, view: WriteView) -> None:
        rows = [
            {
                "concept": "Organization",
                "natural_key": "acme",
                "predicate": "Organization.name",
                "value": "Acme Corp",
                "value_kind": "literal",
            },
            {
                "concept": "Person",
                "natural_key": "ada",
                "predicate": "Person.employer",
                "value": "acme",
                "value_kind": "ref",
            },
        ]
        CsvImporter().import_(rows, view)
        [ref_assertion] = view.assertions(predicate="Person.employer")
        assert ref_assertion.value_kind == "ref"

        [org_entity] = [
            view.get_entity(a.subject) for a in view.assertions(predicate="Organization.name")
        ]
        assert org_entity is not None
        assert ref_assertion.value == org_entity.id

    def test_ref_row_forward_reference_within_file_resolves(self, view: WriteView) -> None:
        """Order-independent: the target's defining row can come AFTER the ref row."""
        rows = [
            {
                "concept": "Person",
                "natural_key": "ada",
                "predicate": "Person.employer",
                "value": "acme",
                "value_kind": "ref",
            },
            {
                "concept": "Organization",
                "natural_key": "acme",
                "predicate": "Organization.name",
                "value": "Acme Corp",
                "value_kind": "literal",
            },
        ]
        report = CsvImporter().import_(rows, view)
        assert report.entities_created == 2
        assert report.assertions_proposed == 2

    def test_ref_row_unresolved_target_raises_validation_error(self, view: WriteView) -> None:
        rows = [
            {
                "concept": "Person",
                "natural_key": "ada",
                "predicate": "Person.employer",
                "value": "nonexistent-key",
                "value_kind": "ref",
            }
        ]
        with pytest.raises(ValidationError, match="was never seen as a subject"):
            CsvImporter().import_(rows, view)

    def test_missing_required_column_raises_validation_error(self, view: WriteView) -> None:
        rows = [{"concept": "Person", "natural_key": "ada", "predicate": "Person.name"}]
        with pytest.raises(ValidationError, match="missing required column"):
            CsvImporter().import_(rows, view)

    def test_invalid_value_kind_raises_validation_error(self, view: WriteView) -> None:
        rows = [
            {
                "concept": "Person",
                "natural_key": "ada",
                "predicate": "Person.name",
                "value": "Ada",
                "value_kind": "bogus",
            }
        ]
        with pytest.raises(ValidationError, match="value_kind must be"):
            CsvImporter().import_(rows, view)

    def test_invalid_confidence_raises_validation_error(self, view: WriteView) -> None:
        rows = [
            {
                "concept": "Person",
                "natural_key": "ada",
                "predicate": "Person.name",
                "value": "Ada",
                "value_kind": "literal",
                "confidence": "not-a-float",
            }
        ]
        with pytest.raises(ValidationError, match="not a valid float"):
            CsvImporter().import_(rows, view)

    def test_empty_rows_returns_zeroed_report(self, view: WriteView) -> None:
        report = CsvImporter().import_([], view)
        assert report == ImportReport(rows_processed=0, entities_created=0, assertions_proposed=0)


class TestImportFromFile:
    def test_imports_from_real_csv_file(self, view: WriteView, tmp_path: Path) -> None:
        csv_path = tmp_path / "people.csv"
        csv_path.write_text(
            "concept,natural_key,predicate,value,value_kind\n"
            "Person,ada,Person.name,Ada Lovelace,literal\n"
        )
        report = CsvImporter().import_(csv_path, view)
        assert report.entities_created == 1
        assert report.assertions_proposed == 1

    def test_unsupported_source_type_raises_validation_error(self, view: WriteView) -> None:
        with pytest.raises(ValidationError, match="Unsupported CSV import source type"):
            CsvImporter().import_(12345, view)
