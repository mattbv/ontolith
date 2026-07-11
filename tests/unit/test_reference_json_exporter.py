"""Unit tests for the JsonExporter reference plugin (KI-010, ADR-0015)."""

import io
import json
import tempfile
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.plugins.reference.json_exporter import ExportReport, JsonExporter
from ontolith.plugins.views import ReadOnlyView, WriteView

PLUGIN_PRINCIPAL = "json-exporter"


@pytest.fixture
def kb() -> Ontology:
    with tempfile.TemporaryDirectory() as tmpdir:
        kb = Ontology.connect(Path(tmpdir) / "test.db")
        # write capability here is a test-setup convenience (auto-accepts the
        # retract() used to build fixture state) - not a claim that a real
        # exporter plugin ever gets more than ReadOnlyView; PluginRegistry
        # hard-caps exporter kind to "read" regardless (see ADR-0015).
        kb.create_principal(
            PLUGIN_PRINCIPAL, kind="service", auth_method="workload", default_capability="write"
        )
        yield kb
        kb.close()


class TestExport:
    def test_empty_kb_exports_empty_array(self, kb: Ontology) -> None:
        view = ReadOnlyView(kb, PLUGIN_PRINCIPAL)
        buf = io.StringIO()
        report = JsonExporter().export(view, buf)
        assert report == ExportReport(assertions_written=0)
        assert json.loads(buf.getvalue()) == []

    def test_exports_active_assertions_as_json(self, kb: Ontology) -> None:
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        entity = write_view.create_entity("Person", natural_key="ada")
        write_view.propose(entity.id, "Person.name", "Ada Lovelace", "Text", confidence=0.95)

        buf = io.StringIO()
        report = JsonExporter().export(ReadOnlyView(kb, PLUGIN_PRINCIPAL), buf)
        assert report.assertions_written == 1

        [record] = json.loads(buf.getvalue())
        assert record["subject"] == entity.id
        assert record["predicate"] == "Person.name"
        assert record["value"] == "Ada Lovelace"
        assert record["confidence"] == 0.95

    def test_excludes_retracted_assertions(self, kb: Ontology) -> None:
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        entity = write_view.create_entity("Person", natural_key="ada")
        write_view.propose(entity.id, "Person.name", "Ada Lovelace", "Text")
        [assertion] = write_view.assertions(predicate="Person.name")
        write_view.retract(assertion.id)

        buf = io.StringIO()
        report = JsonExporter().export(ReadOnlyView(kb, PLUGIN_PRINCIPAL), buf)
        assert report.assertions_written == 0

    def test_exports_to_real_file_path(self, kb: Ontology, tmp_path: Path) -> None:
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        entity = write_view.create_entity("Person", natural_key="ada")
        write_view.propose(entity.id, "Person.name", "Ada Lovelace", "Text")

        out_path = tmp_path / "export.json"
        report = JsonExporter().export(ReadOnlyView(kb, PLUGIN_PRINCIPAL), out_path)
        assert report.assertions_written == 1
        assert json.loads(out_path.read_text())[0]["value"] == "Ada Lovelace"

    def test_exports_to_real_file_path_as_string(self, kb: Ontology, tmp_path: Path) -> None:
        out_path = str(tmp_path / "export.json")
        report = JsonExporter().export(ReadOnlyView(kb, PLUGIN_PRINCIPAL), out_path)
        assert report.assertions_written == 0
        assert json.loads(Path(out_path).read_text()) == []

    def test_unsupported_target_type_raises_type_error(self, kb: Ontology) -> None:
        with pytest.raises(TypeError, match="Unsupported JSON export target type"):
            JsonExporter().export(ReadOnlyView(kb, PLUGIN_PRINCIPAL), 12345)
