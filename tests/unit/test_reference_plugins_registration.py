"""Capstone: registers the real reference plugins via PluginRegistry using
UNPATCHED importlib.metadata entry-point discovery (KI-010, ADR-0015).

Unlike tests/unit/test_plugin_registry.py (which monkeypatches entry_points
to point at tests/fixtures/plugins/ test doubles), this proves the actual
pyproject.toml "ontolith.plugins" entry-point wiring resolves for real,
installed reference plugins - discovery, manifest, capability negotiation,
and view-scoped writes/reads all working end to end with production code.

Since ADR-0051, register() defaults to isolate=True: loaded.instance is an
IsolatedPluginProxy, not the real plugin object, so isinstance(loaded.
instance, CsvImporter) no longer holds (loaded.instance.plugin_class is
CsvImporter is the replacement check). The end-to-end tests below call
loaded.instance.import_(...)/.export(...) exactly as they did before
ADR-0051 - these are this project's real regression tests that the
sandboxed subprocess round-trip is transparent to a caller that doesn't
know or care isolation is happening underneath.
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
from ontolith.plugins.sandbox.runner import IsolatedPluginProxy
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
        assert isinstance(loaded.instance, IsolatedPluginProxy)
        assert loaded.instance.plugin_class is CsvImporter
        assert isinstance(loaded.view, WriteView)
        assert loaded.manifest.name == "csv-importer"

    def test_json_exporter_registers_via_real_entry_point(self, kb: Ontology) -> None:
        loaded = PluginRegistry(kb).register("json-exporter", author=ADMIN)
        assert isinstance(loaded.instance, IsolatedPluginProxy)
        assert loaded.instance.plugin_class is JsonExporter
        assert isinstance(loaded.view, ReadOnlyView)
        assert not isinstance(loaded.view, WriteView)

    def test_required_fields_validator_registers_via_real_entry_point(self, kb: Ontology) -> None:
        loaded = PluginRegistry(kb).register("required-fields-validator", author=ADMIN)
        assert isinstance(loaded.instance, IsolatedPluginProxy)
        assert loaded.instance.plugin_class is RequiredFieldsValidator
        assert isinstance(loaded.view, ReadOnlyView)
        assert not isinstance(loaded.view, WriteView)

    def test_rdf_owl_exporter_registers_via_real_entry_point(self, kb: Ontology) -> None:
        loaded = PluginRegistry(kb).register("rdf-owl-exporter", author=ADMIN)
        assert isinstance(loaded.instance, IsolatedPluginProxy)
        assert loaded.instance.plugin_class is RdfExporter
        assert isinstance(loaded.view, ReadOnlyView)
        assert not isinstance(loaded.view, WriteView)

    def test_isolate_false_returns_the_real_instance_directly(self, kb: Ontology) -> None:
        """The documented escape hatch (ADR-0051): with isolate=False,
        loaded.instance is the real, directly-instantiated plugin object,
        exactly as every registration behaved before this ADR."""
        loaded = PluginRegistry(kb).register(
            "csv-importer", author=ADMIN, granted_capability="write", isolate=False
        )
        assert isinstance(loaded.instance, CsvImporter)


class TestEndToEndThroughRegistry:
    def test_csv_importer_end_to_end_via_registry_view(self, kb: Ontology) -> None:
        loaded = PluginRegistry(kb).register(
            "csv-importer", author=ADMIN, granted_capability="write"
        )
        assert isinstance(loaded.view, WriteView)
        assert isinstance(loaded.instance, IsolatedPluginProxy)
        assert loaded.instance.plugin_class is CsvImporter

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
        # A dataclass return value crosses an isolated call as a plain dict
        # (wire.to_wire_result, security review finding) - isolate=False
        # would return the real ImportReport instance unchanged.
        assert report["entities_created"] == 1
        assert report["assertions_proposed"] == 1

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
        assert isinstance(loaded.instance, IsolatedPluginProxy)
        assert loaded.instance.plugin_class is RdfExporter

        buf = io.StringIO()
        report = loaded.instance.export(loaded.view, buf)
        assert report["entities_written"] == 1
        assert report["assertions_written"] == 1
        assert "Ada Lovelace" in buf.getvalue()
