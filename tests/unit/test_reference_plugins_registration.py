"""Capstone: registers the real reference plugins via PluginRegistry using
UNPATCHED importlib.metadata entry-point discovery (KI-010, ADR-0015).

Unlike tests/unit/test_plugin_registry.py (which monkeypatches entry_points
to point at tests/fixtures/plugins/ test doubles), this proves the actual
pyproject.toml "ontolith.plugins" entry-point wiring resolves for real,
installed reference plugins - discovery, manifest, capability negotiation,
and view-scoped writes/reads all working end to end with production code.
"""

import tempfile
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.plugins.reference.csv_importer import CsvImporter
from ontolith.plugins.reference.json_exporter import JsonExporter
from ontolith.plugins.reference.rdf_exporter import RdfExporter
from ontolith.plugins.reference.required_fields_validator import RequiredFieldsValidator
from ontolith.plugins.registry import PluginRegistry
from ontolith.plugins.views import ReadOnlyView, WriteView

ADMIN = "admin@example.com"


@pytest.fixture
def kb() -> Ontology:
    with tempfile.TemporaryDirectory() as tmpdir:
        kb = Ontology.connect(Path(tmpdir) / "test.db")
        kb.create_principal(ADMIN, kind="human", default_capability="admin")
        yield kb
        kb.close()


class TestRealEntryPointDiscovery:
    def test_csv_importer_registers_via_real_entry_point(self, kb: Ontology) -> None:
        loaded = PluginRegistry(kb).register(
            "csv-importer", author=ADMIN, granted_capability="write"
        )
        assert isinstance(loaded.instance, CsvImporter)
        assert isinstance(loaded.view, WriteView)
        assert loaded.manifest.name == "csv-importer"

    def test_json_exporter_registers_via_real_entry_point(self, kb: Ontology) -> None:
        loaded = PluginRegistry(kb).register("json-exporter", author=ADMIN)
        assert isinstance(loaded.instance, JsonExporter)
        assert isinstance(loaded.view, ReadOnlyView)
        assert not isinstance(loaded.view, WriteView)

    def test_required_fields_validator_registers_via_real_entry_point(self, kb: Ontology) -> None:
        loaded = PluginRegistry(kb).register("required-fields-validator", author=ADMIN)
        assert isinstance(loaded.instance, RequiredFieldsValidator)
        assert isinstance(loaded.view, ReadOnlyView)
        assert not isinstance(loaded.view, WriteView)

    def test_rdf_owl_exporter_registers_via_real_entry_point(self, kb: Ontology) -> None:
        loaded = PluginRegistry(kb).register("rdf-owl-exporter", author=ADMIN)
        assert isinstance(loaded.instance, RdfExporter)
        assert isinstance(loaded.view, ReadOnlyView)
        assert not isinstance(loaded.view, WriteView)


class TestEndToEndThroughRegistry:
    def test_csv_importer_end_to_end_via_registry_view(self, kb: Ontology) -> None:
        loaded = PluginRegistry(kb).register(
            "csv-importer", author=ADMIN, granted_capability="write"
        )
        assert isinstance(loaded.view, WriteView)
        assert isinstance(loaded.instance, CsvImporter)

        rows = [
            {
                "concept": "Person",
                "natural_key": "ada",
                "predicate": "Person.name",
                "value": "Ada Lovelace",
                "value_kind": "literal",
            }
        ]
        report = loaded.instance.import_(rows, loaded.view)
        assert report.entities_created == 1
        assert report.assertions_proposed == 1

        [assertion] = loaded.view.assertions(predicate="Person.name")
        assert assertion.author == loaded.principal_id
        assert assertion.value == "Ada Lovelace"

    def test_rdf_owl_exporter_end_to_end_via_registry_view(self, kb: Ontology) -> None:
        import io

        from ontolith.schema import ConceptDef, PropertyDef, SchemaIR

        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person", properties={"name": PropertyDef(name="name", value_type="Text")}
                )
            },
        )
        kb.apply_schema(schema, author=ADMIN)
        entity = kb.create_entity("Person", author=ADMIN)
        kb.assert_literal(entity.id, "Person.name", "Ada Lovelace", "Text", ADMIN)

        loaded = PluginRegistry(kb).register("rdf-owl-exporter", author=ADMIN)
        assert isinstance(loaded.view, ReadOnlyView)
        assert isinstance(loaded.instance, RdfExporter)

        buf = io.StringIO()
        report = loaded.instance.export(loaded.view, buf)
        assert report.entities_written == 1
        assert report.assertions_written == 1
        assert "Ada Lovelace" in buf.getvalue()
