"""RDF/OWL exporter reference plugin (SPEC §13.3, ADR-0036).

Demonstrates the Exporter protocol against a real ReadOnlyView: writes the
active schema as an OWL ontology (`schema.rdf.to_owl`) plus every
currently-active assertion as RDF instance data — each distinct subject
becomes an individual typed `rdf:type` its entity's concept class, each
literal assertion becomes a datatype-property triple (XSD-typed per
`value_type_to_xsd`), each ref assertion becomes an object-property triple
— into one graph, then serializes it (Turtle by default; any `rdflib`
output format works).

Deliberately NOT imported by `plugins/reference/__init__.py`, unlike the
other reference plugins there — `rdflib` is an optional `interop`-extra
dependency, and `reference/__init__.py`'s existing eager imports mean
importing *any* reference plugin (including via its own entry point, which
still initializes the parent package first) would otherwise require
`rdflib` installed even for a deployment using only CsvImporter/JsonExporter/
RequiredFieldsValidator. Import this module directly:
`from ontolith.plugins.reference.rdf_exporter import RdfExporter`.

Only entities that own at least one active assertion are exported as
individuals — an entity with zero active assertions never appears in
`kb.assertions()`'s iteration, so it's never seen here. Matches
`JsonExporter`'s own assertion-driven scope (not an entity-driven export);
not a new limitation this plugin introduces.

Every property/relation IRI a written triple actually uses is declared its
own `owl:DatatypeProperty`/`owl:ObjectProperty` type here too, even for a
predicate the *current* schema no longer declares at all (no
migration/backfill mechanism exists, KI-048, so an already-active
assertion under a since-removed predicate is still reachable) — OWL 2 DL
requires a declaration for every property IRI used, and `to_owl()` alone
only declares what the current schema still has. This does NOT make a
*retyped* predicate (still declared, but as a relation where it used to be
a property, or vice versa — `apply_schema` doesn't reject this) DL-valid:
an old literal assertion under a predicate now schema-declared a relation
still gets declared `owl:DatatypeProperty` here (from the assertion's own
`value_kind`) alongside `owl:ObjectProperty` (from `to_owl()`'s current
schema read), and punning between the two is itself prohibited in OWL 2
DL. Not a regression this fix introduces — that data was already
non-DL-valid before this fix (a literal object under a schema-declared
`owl:ObjectProperty`) — just not fully closed by it either.
"""

from dataclasses import dataclass
from pathlib import Path

from rdflib import OWL, RDF
from rdflib import Literal as RdfLiteral

from ontolith.plugins.manifest import PluginCapabilities, PluginManifest
from ontolith.plugins.views import ReadOnlyView
from ontolith.schema.rdf import (
    iri_for_concept,
    iri_for_entity,
    iri_for_property,
    to_owl,
    value_type_to_xsd,
)


@dataclass(frozen=True)
class RdfExportReport:
    """Summary of a completed RDF/OWL export.

    Attributes:
        entities_written: Distinct subjects that received an `rdf:type`
            triple — i.e. actually resolved via `kb.get_entity()`. Not the
            same as "distinct subjects seen": a subject whose entity
            somehow doesn't resolve (unreachable in practice, see
            `RdfExporter.export`'s own comment) still gets its property
            triples written but doesn't count here.
        assertions_written: Active assertions written as property triples.
    """

    entities_written: int
    assertions_written: int


class RdfExporter:
    """Reference Exporter plugin: serializes the schema (as OWL) and active
    assertions (as RDF instance data) to a single RDF document."""

    manifest = PluginManifest(
        name="rdf-owl-exporter",
        version="0.1.0",
        kind="exporter",
        capabilities=PluginCapabilities(filesystem=True),
    )

    def export(
        self, kb: ReadOnlyView, target: object, *, format: str = "turtle"
    ) -> RdfExportReport:
        """Write the schema (OWL) and active assertions (RDF instances) to `target`.

        Args:
            kb: Read-only KB view.
            target: A path (str/Path, opened for writing) or any writable
                text-mode file-like object (e.g. io.StringIO for tests).
            format: `rdflib` serialization format — Turtle by default.

        Returns:
            Counts of entities and assertions written.

        Raises:
            ValueError: No schema is registered for this namespace — there
                is nothing to derive OWL classes/properties from.
            TypeError: `target` is neither path-like nor writable.
        """
        schema = kb.schema()
        if schema is None:
            raise ValueError("Cannot export RDF/OWL: no schema registered for this namespace")

        graph = to_owl(schema)

        entities_typed: set[str] = set()
        entities_probed: set[str] = set()
        assertions_written = 0
        for assertion in kb.assertions():
            subject_iri = iri_for_entity(schema.namespace, assertion.subject)
            if assertion.subject not in entities_probed:
                entities_probed.add(assertion.subject)
                entity = kb.get_entity(assertion.subject)
                # entity is always found in practice (assertion.subject has
                # a real FK to entity.id) - degrades gracefully rather than
                # raising if it somehow isn't: the rdf:type triple (and the
                # entities_written count) is skipped, but the property
                # triple below still gets written.
                if entity is not None:
                    graph.add(
                        (subject_iri, RDF.type, iri_for_concept(schema.namespace, entity.concept))
                    )
                    entities_typed.add(assertion.subject)

            predicate_iri = iri_for_property(schema.namespace, assertion.predicate)
            # OWL 2 DL requires a declaration for every property IRI used -
            # to_owl() only declares predicates the *current* schema still
            # has, so a predicate a later schema migration removed entirely
            # (no migration/backfill mechanism exists, so an old assertion
            # under a since-removed predicate can still be active) would
            # otherwise be used here with no declaration anywhere in the
            # graph. Declaring it here too is a no-op for the common case
            # where it's already declared (RDF graphs are sets).
            if assertion.value_kind == "literal":
                assert assertion.value_type is not None  # required for value_kind="literal"
                graph.add((predicate_iri, RDF.type, OWL.DatatypeProperty))
                datatype = value_type_to_xsd(assertion.value_type)
                graph.add(
                    (subject_iri, predicate_iri, RdfLiteral(assertion.value, datatype=datatype))
                )
            else:
                graph.add((predicate_iri, RDF.type, OWL.ObjectProperty))
                graph.add(
                    (subject_iri, predicate_iri, iri_for_entity(schema.namespace, assertion.value))
                )
            assertions_written += 1

        serialized = graph.serialize(format=format)
        if hasattr(target, "write"):
            target.write(serialized)
        elif isinstance(target, (str, Path)):
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(serialized)
        else:
            raise TypeError(f"Unsupported RDF export target type: {type(target).__name__}")

        return RdfExportReport(
            entities_written=len(entities_typed), assertions_written=assertions_written
        )


__all__ = ["RdfExporter", "RdfExportReport"]
