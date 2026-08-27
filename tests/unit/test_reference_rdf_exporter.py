"""Unit tests for the RdfExporter reference plugin (SPEC §13.3, ADR-0036)."""

import io
import tempfile
from pathlib import Path

import pytest
from rdflib import OWL, RDF, XSD, Graph

from ontolith import Ontology
from ontolith.plugins.reference.rdf_exporter import RdfExporter, RdfExportReport
from ontolith.plugins.views import ReadOnlyView, WriteView
from ontolith.schema import ConceptDef, PropertyDef, RelationDef, SchemaIR
from ontolith.schema.rdf import iri_for_concept, iri_for_entity, iri_for_property

PLUGIN_PRINCIPAL = "rdf-owl-exporter"
ADMIN = "admin@example.com"


@pytest.fixture
def kb() -> Ontology:
    with tempfile.TemporaryDirectory() as tmpdir:
        kb = Ontology.connect(Path(tmpdir) / "test.db")
        kb.create_principal(
            PLUGIN_PRINCIPAL, kind="service", auth_method="workload", default_capability="write"
        )
        kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
        yield kb
        kb.close()


def _apply_schema(kb: Ontology) -> None:
    schema = SchemaIR(
        namespace="default",
        version=1,
        concepts={
            "Organization": ConceptDef(name="Organization"),
            "Person": ConceptDef(
                name="Person",
                properties={"name": PropertyDef(name="name", value_type="Text")},
                relations={"employer": RelationDef(name="employer", target_concept="Organization")},
            ),
        },
    )
    kb.apply_schema(schema, author=ADMIN)


class TestExport:
    def test_no_schema_raises_value_error(self, kb: Ontology) -> None:
        view = ReadOnlyView(kb, PLUGIN_PRINCIPAL)
        with pytest.raises(ValueError, match="no schema registered"):
            RdfExporter().export(view, io.StringIO())

    def test_schema_only_kb_exports_ontology_with_no_individuals(self, kb: Ontology) -> None:
        _apply_schema(kb)
        view = ReadOnlyView(kb, PLUGIN_PRINCIPAL)
        buf = io.StringIO()
        report = RdfExporter().export(view, buf)
        assert report == RdfExportReport(entities_written=0, assertions_written=0)

        graph = Graph()
        graph.parse(data=buf.getvalue(), format="turtle")
        assert (iri_for_concept("default", "Person"), RDF.type, OWL.Class) in graph

    def test_exports_entity_and_literal_assertion(self, kb: Ontology) -> None:
        _apply_schema(kb)
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        entity = write_view.create_entity("Person")
        write_view.propose(entity.id, "Person.name", "Ada", "Text")

        buf = io.StringIO()
        report = RdfExporter().export(ReadOnlyView(kb, PLUGIN_PRINCIPAL), buf)
        assert report == RdfExportReport(entities_written=1, assertions_written=1)

        graph = Graph()
        graph.parse(data=buf.getvalue(), format="turtle")
        entity_iri = iri_for_entity("default", entity.id)
        assert (entity_iri, RDF.type, iri_for_concept("default", "Person")) in graph
        assert (
            entity_iri,
            iri_for_property("default", "Person.name"),
            None,
        ) in graph
        [triple] = list(
            graph.triples((entity_iri, iri_for_property("default", "Person.name"), None))
        )
        literal = triple[2]
        assert str(literal) == "Ada"
        assert literal.datatype == XSD.string  # type: ignore[union-attr]

    def test_exports_ref_assertion_as_object_property_triple(self, kb: Ontology) -> None:
        """`org` owns no assertions of its own (only `person` does, the ref
        assertion's subject) - only its IRI appears, as the triple's
        object, not counted in entities_written (which counts distinct
        subjects seen, matching the documented "assertion-driven, not
        entity-driven" export scope)."""
        _apply_schema(kb)
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        org = write_view.create_entity("Organization")
        person = write_view.create_entity("Person")
        write_view.propose_ref(person.id, "Person.employer", org.id)

        buf = io.StringIO()
        report = RdfExporter().export(ReadOnlyView(kb, PLUGIN_PRINCIPAL), buf)
        assert report == RdfExportReport(entities_written=1, assertions_written=1)

        graph = Graph()
        graph.parse(data=buf.getvalue(), format="turtle")
        assert (
            iri_for_entity("default", person.id),
            iri_for_property("default", "Person.employer"),
            iri_for_entity("default", org.id),
        ) in graph
        # org itself has no rdf:type triple - it's never a subject, only
        # referenced as an object, matching the documented
        # assertion-driven (not entity-driven) export scope.
        assert not any(graph.triples((iri_for_entity("default", org.id), RDF.type, None)))

    def test_excludes_retracted_assertions(self, kb: Ontology) -> None:
        _apply_schema(kb)
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        entity = write_view.create_entity("Person")
        write_view.propose(entity.id, "Person.name", "Ada", "Text")
        [assertion] = write_view.assertions(predicate="Person.name")
        write_view.retract(assertion.id)

        buf = io.StringIO()
        report = RdfExporter().export(ReadOnlyView(kb, PLUGIN_PRINCIPAL), buf)
        assert report == RdfExportReport(entities_written=0, assertions_written=0)

    def test_exports_to_real_file_path(self, kb: Ontology, tmp_path: Path) -> None:
        _apply_schema(kb)
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        entity = write_view.create_entity("Person")
        write_view.propose(entity.id, "Person.name", "Ada", "Text")

        out_path = tmp_path / "export.ttl"
        report = RdfExporter().export(ReadOnlyView(kb, PLUGIN_PRINCIPAL), out_path)
        assert report.assertions_written == 1

        graph = Graph()
        graph.parse(str(out_path), format="turtle")
        assert len(graph) > 0

    def test_exports_to_real_file_path_as_string(self, kb: Ontology, tmp_path: Path) -> None:
        _apply_schema(kb)
        out_path = str(tmp_path / "export.ttl")
        report = RdfExporter().export(ReadOnlyView(kb, PLUGIN_PRINCIPAL), out_path)
        assert report.assertions_written == 0
        graph = Graph()
        graph.parse(out_path, format="turtle")
        assert len(graph) > 0

    def test_unsupported_target_type_raises_type_error(self, kb: Ontology) -> None:
        _apply_schema(kb)
        with pytest.raises(TypeError, match="Unsupported RDF export target type"):
            RdfExporter().export(ReadOnlyView(kb, PLUGIN_PRINCIPAL), 12345)

    def test_alternate_serialization_format(self, kb: Ontology) -> None:
        _apply_schema(kb)
        write_view = WriteView(kb, PLUGIN_PRINCIPAL)
        entity = write_view.create_entity("Person")
        write_view.propose(entity.id, "Person.name", "Ada", "Text")

        buf = io.StringIO()
        RdfExporter().export(ReadOnlyView(kb, PLUGIN_PRINCIPAL), buf, format="xml")
        graph = Graph()
        graph.parse(data=buf.getvalue(), format="xml")
        assert (
            iri_for_entity("default", entity.id),
            RDF.type,
            iri_for_concept("default", "Person"),
        ) in graph
