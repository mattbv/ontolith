"""Plugin contract kit (SPEC §13, ADR-0015) — M3 exit criterion "plugin
contract tests green" (Implementation Plan §2).

Behavioral contracts every plugin of a given kind must satisfy, independent
of implementation — analogous to conformance/conftest.py's `make_kb`
parametrizing StorageBackend vectors over sqlite/duckdb so backends can
self-certify. Plugins of the same kind are grouped in a `_..._PLUGINS`
mapping below; adding a second plugin of an existing kind means adding one
entry (plus a matching sample-source/target helper where one is needed), not
writing a new test class.

Implementation-specific behavior (CSV column parsing, JSON serialization
shape, required-predicate rule content, ...) belongs in each plugin's own
tests/unit/test_reference_*.py, not here. This file only asserts what the
Importer/Exporter/Validator Protocols themselves promise (plugins/ports.py).

All plugins here are loaded via real, installed `ontolith.plugins` entry
points (pyproject.toml) through the production PluginRegistry.register()
path — no entry-point monkeypatching — so the contract is proven against
the same discovery mechanism a real deployment uses.
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.plugins.reference.csv_importer import CsvImporter
from ontolith.plugins.reference.json_exporter import JsonExporter
from ontolith.plugins.reference.required_fields_validator import RequiredFieldsValidator
from ontolith.plugins.registry import LoadedPlugin, PluginRegistry
from ontolith.plugins.views import ReadOnlyView, WriteView

ADMIN = "admin@example.com"

_IMPORTER_PLUGINS: dict[str, type] = {"csv-importer": CsvImporter}
_EXPORTER_PLUGINS: dict[str, type] = {"json-exporter": JsonExporter}
_VALIDATOR_PLUGINS: dict[str, type] = {"required-fields-validator": RequiredFieldsValidator}

# One minimal, valid `source` per registered Importer — producing exactly
# one entity and one assertion — since the Protocol's `source: object` is
# deliberately opaque and has no shape a generic test could synthesize.
_SAMPLE_IMPORT_SOURCE: dict[str, object] = {
    "csv-importer": [
        {
            "concept": "Person",
            "natural_key": "ada",
            "predicate": "Person.name",
            "value": "Ada Lovelace",
            "value_kind": "literal",
        }
    ],
}


@pytest.fixture
def kb() -> Ontology:
    with tempfile.TemporaryDirectory() as tmpdir:
        kb = Ontology.connect(Path(tmpdir) / "test.db")
        kb.create_principal(ADMIN, kind="human", default_capability="admin")
        yield kb
        kb.close()


def _seed_one_assertion(kb: Ontology) -> tuple[str, str]:
    """Create one entity with one active assertion; return (entity_id, predicate)."""
    entity = kb.create_entity("Person", author=ADMIN)
    assertion = kb.assert_literal(entity.id, "Person.name", "Ada Lovelace", "Text", author=ADMIN)
    return entity.id, assertion.predicate


# ===========================================================================
# Importer contract
# ===========================================================================


@pytest.fixture(params=sorted(_IMPORTER_PLUGINS))
def loaded_importer(kb: Ontology, request: pytest.FixtureRequest) -> LoadedPlugin:
    return PluginRegistry(kb).register(request.param, author=ADMIN, granted_capability="write")


class TestImporterContract:
    def test_manifest_kind_is_importer(self, loaded_importer: LoadedPlugin) -> None:
        assert loaded_importer.manifest.kind == "importer"

    def test_view_is_a_write_view(self, loaded_importer: LoadedPlugin) -> None:
        assert isinstance(loaded_importer.view, WriteView)

    def test_import_returns_a_report_without_raising(self, loaded_importer: LoadedPlugin) -> None:
        source = _SAMPLE_IMPORT_SOURCE[loaded_importer.manifest.name]
        report = loaded_importer.instance.import_(source, loaded_importer.view)
        assert report is not None

    def test_import_writes_are_attributed_to_the_plugin_principal(
        self, loaded_importer: LoadedPlugin, kb: Ontology
    ) -> None:
        source = _SAMPLE_IMPORT_SOURCE[loaded_importer.manifest.name]
        loaded_importer.instance.import_(source, loaded_importer.view)

        active = kb.assertions(status="active")
        assert active, "import_ of a valid sample source must produce at least one assertion"
        assert all(a.author == loaded_importer.principal_id for a in active), (
            "every assertion an Importer produces must be attributed to the plugin's own "
            "registered principal, never a different or spoofed author"
        )

    def test_import_writes_are_visible_through_the_plugin_own_view(
        self, loaded_importer: LoadedPlugin, kb: Ontology
    ) -> None:
        """Proves the write actually went through governance (WriteView.propose),
        not some other path invisible to the view that produced it."""
        source = _SAMPLE_IMPORT_SOURCE[loaded_importer.manifest.name]
        loaded_importer.instance.import_(source, loaded_importer.view)

        via_view = {a.id for a in loaded_importer.view.assertions(status="active")}
        via_kb = {a.id for a in kb.assertions(status="active")}
        assert via_view == via_kb


# ===========================================================================
# Exporter contract
# ===========================================================================


@pytest.fixture(params=sorted(_EXPORTER_PLUGINS))
def loaded_exporter(kb: Ontology, request: pytest.FixtureRequest) -> LoadedPlugin:
    return PluginRegistry(kb).register(request.param, author=ADMIN)


class TestExporterContract:
    def test_manifest_kind_is_exporter(self, loaded_exporter: LoadedPlugin) -> None:
        assert loaded_exporter.manifest.kind == "exporter"

    def test_view_is_read_only_not_a_write_view(self, loaded_exporter: LoadedPlugin) -> None:
        assert isinstance(loaded_exporter.view, ReadOnlyView)
        assert not isinstance(loaded_exporter.view, WriteView)

    def test_export_does_not_mutate_kb_state(
        self, loaded_exporter: LoadedPlugin, kb: Ontology
    ) -> None:
        _seed_one_assertion(kb)
        before = {a.id for a in kb.assertions(status="active")}

        loaded_exporter.instance.export(loaded_exporter.view, io.StringIO())

        after = {a.id for a in kb.assertions(status="active")}
        assert before == after

    def test_export_produces_nonempty_output_when_kb_has_data(
        self, loaded_exporter: LoadedPlugin, kb: Ontology
    ) -> None:
        _seed_one_assertion(kb)
        target = io.StringIO()

        report = loaded_exporter.instance.export(loaded_exporter.view, target)

        assert report is not None
        assert target.getvalue() != ""


# ===========================================================================
# Validator contract
# ===========================================================================


@pytest.fixture(params=sorted(_VALIDATOR_PLUGINS))
def loaded_validator(kb: Ontology, request: pytest.FixtureRequest) -> LoadedPlugin:
    return PluginRegistry(kb).register(request.param, author=ADMIN)


class TestValidatorContract:
    def test_manifest_kind_is_validator(self, loaded_validator: LoadedPlugin) -> None:
        assert loaded_validator.manifest.kind == "validator"

    def test_view_is_read_only_not_a_write_view(self, loaded_validator: LoadedPlugin) -> None:
        assert isinstance(loaded_validator.view, ReadOnlyView)
        assert not isinstance(loaded_validator.view, WriteView)

    def test_validate_returns_a_list_of_strings(
        self, loaded_validator: LoadedPlugin, kb: Ontology
    ) -> None:
        entity_id, predicate = _seed_one_assertion(kb)
        [assertion] = kb.assertions(subject=entity_id, predicate=predicate, status="active")

        result = loaded_validator.instance.validate(assertion, loaded_validator.view)

        assert isinstance(result, list)
        assert all(isinstance(item, str) for item in result)

    def test_validate_does_not_mutate_kb_state(
        self, loaded_validator: LoadedPlugin, kb: Ontology
    ) -> None:
        entity_id, predicate = _seed_one_assertion(kb)
        [assertion] = kb.assertions(subject=entity_id, predicate=predicate, status="active")
        before = {a.id for a in kb.assertions(status="active")}

        loaded_validator.instance.validate(assertion, loaded_validator.view)

        after = {a.id for a in kb.assertions(status="active")}
        assert before == after
