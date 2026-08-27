"""RDF/OWL bridge — schema half (SPEC §13.3, ADR-0036).

`to_owl(schema)` translates a `SchemaIR` into an OWL ontology as an
`rdflib.Graph` — concepts become `owl:Class`, properties become
`owl:DatatypeProperty` (range mapped to XSD), relations become
`owl:ObjectProperty` (range the target concept's class), matching SPEC
§13.3's "RDF/OWL bridge MAY initially delegate to LinkML's RDF emission" by
producing the same shape of output LinkML's own RDF generation would,
without depending on the real `linkml`/`linkml-runtime` packages (ADR-0013
already rejected that for the YAML bridge, for the same dependency-weight
reason).

Schema-only, mirroring `schema/linkml.py`'s `to_yaml` — instance data
(entities/assertions as RDF individuals) is a separate concern, handled by
the `RdfExporter` reference plugin (`plugins/reference/rdf_exporter.py`),
which reuses this module's IRI-minting helpers so schema and instance IRIs
are consistently derived from the same scheme.

Not imported from `schema/__init__.py` — `rdflib` is an optional
`interop`-extra dependency (mirrors `schema/linkml.py`'s own `pyyaml`
opt-in, which for the identical reason isn't re-exported from the package
root either). Import directly: `from ontolith.schema.rdf import to_owl`.

Export only — there is no `from_owl`/import direction in v1. Reading
arbitrary external OWL (unbounded expressivity: class hierarchies,
restrictions, unions, etc.) back into Ontolith's own deliberately simple
property/relation model is a substantially larger, separate problem than
emitting it, the same class of scope boundary ADR-0013 already drew for the
LinkML dialect (a documented, deliberately-scoped subset, not full
conformance) — left as an explicit future decision, not silently dropped.
"""

from __future__ import annotations

from urllib.parse import quote

from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef

from ontolith.schema.ir import SchemaIR

# XSD datatype for each Ontolith value_type. JSON maps to xsd:string rather
# than a JSON-specific RDF datatype (e.g. rdf:JSON) - mirrors
# schema/linkml.py's own `_VALUE_TYPE_TO_LINKML_RANGE`, which makes the
# identical simplification for the identical reason: no dedicated
# structured-data range in the target dialect's core type set.
_VALUE_TYPE_TO_XSD: dict[str, URIRef] = {
    "Text": XSD.string,
    "Integer": XSD.integer,
    "Float": XSD.double,
    "Boolean": XSD.boolean,
    "Date": XSD.date,
    "DateTime": XSD.dateTime,
    "URI": XSD.anyURI,
    "JSON": XSD.string,
}


def value_type_to_xsd(value_type: str) -> URIRef:
    """Map an Ontolith `value_type` token to its XSD datatype IRI.

    Args:
        value_type: One of SPEC §4's closed eight value_type tokens.

    Returns:
        The corresponding `xsd:` datatype IRI.

    Raises:
        KeyError: `value_type` is not one of the closed eight - defensive
            only, since every literal assertion's `value_type` is already
            validated against that closed set at write time (KI-031).
    """
    return _VALUE_TYPE_TO_XSD[value_type]


def base_iri(namespace: str) -> str:
    """Deterministic base IRI for `namespace`.

    A `urn:` IRI, not an `http(s):` one - Ontolith has no fixed, resolvable
    public host to mint real web IRIs under, and fabricating one (e.g.
    `https://ontolith.example/...`) would look like a real, dereferenceable
    domain when it isn't. `urn:` IRIs are valid RDF/OWL subjects and
    predicates without implying network resolvability.

    Deliberately does not consult `SchemaIR.metadata["default_prefix"]`/
    `["prefixes"]` (preserved verbatim from a LinkML-sourced schema,
    ADR-0013) even though they exist for exactly this purpose - v1 keeps
    IRI minting simple and namespace-derived only; honoring an imported
    LinkML schema's own declared prefixes for RDF/OWL export is a
    reasonable follow-up, not implemented here.

    `namespace` is percent-encoded (`urllib.parse.quote`, default safe
    set) before being embedded - `SchemaIR.namespace`/`ConceptDef.name`/
    `PropertyDef.name`/`RelationDef.name` are unconstrained `str` in the
    IR (schema/ir.py has no character-set validation), and the class-DSL/
    ULID-entity-id paths that happen to produce IRI-safe strings today
    aren't the only way to build a `SchemaIR` - a LinkML-imported schema
    (schema/linkml.py::from_yaml) takes names verbatim from YAML keys,
    which idiomatically include spaces (`"named thing"`) or a URL `id:`,
    either of which breaks unescaped `urn:` NSS/Turtle IRI syntax (found
    in review: reproduced an actual rdflib serialization crash on a
    realistic LinkML document). Percent-encoding every name component
    closes this for every caller of this module's IRI helpers, not just
    the ones already known to be safe.
    """
    return f"urn:ontolith:{quote(namespace, safe='')}:"


def iri_for_concept(namespace: str, concept_name: str) -> URIRef:
    """IRI for a concept's OWL class."""
    return URIRef(f"{base_iri(namespace)}class:{quote(concept_name, safe='')}")


def iri_for_property(namespace: str, predicate: str) -> URIRef:
    """IRI for a property/relation's OWL property.

    `predicate` is the same dotted `"Concept.field"` string used
    everywhere else in this codebase (`Assertion.predicate`,
    `SchemaIR.value_type_of()`, ...) - reusing it here keeps property IRIs
    directly traceable back to the predicate that produced the triple.
    Percent-encoded as one unit (not per dotted segment) - `.` is always
    left unescaped by `urllib.parse.quote` (RFC 3986 unreserved), so the
    dotted structure survives encoding intact.
    """
    return URIRef(f"{base_iri(namespace)}property:{quote(predicate, safe='')}")


def iri_for_entity(namespace: str, entity_id: str) -> URIRef:
    """IRI for an entity's RDF individual."""
    return URIRef(f"{base_iri(namespace)}entity:{quote(entity_id, safe='')}")


def to_owl(schema: SchemaIR) -> Graph:
    """Translate `schema` into an OWL ontology graph (SPEC §13.3).

    Each concept becomes an `owl:Class`. Each property becomes an
    `owl:DatatypeProperty` with `rdfs:domain` the declaring concept's class
    and `rdfs:range` the XSD datatype `value_type_to_xsd` maps its
    `value_type` to. Each relation becomes an `owl:ObjectProperty` with
    `rdfs:domain`/`rdfs:range` the declaring/target concepts' classes, and
    `owl:inverseOf` the target concept's own `inverse`-named property IRI
    if one is declared (emitted as a forward reference — the schema layer
    doesn't require the inverse side to independently declare the same
    relation back, so the referenced IRI may not itself appear elsewhere in
    the graph; this is valid RDF, just not necessarily a *populated*
    property in this specific export).

    `cardinality="single"` **and** `temporality="static"` (both the schema
    defaults, ADR-0017/SPEC §10.1) are additionally typed
    `owl:FunctionalProperty` — the standard OWL idiom for "at most one
    value", requiring no cardinality-restriction blank nodes.
    `temporality="time_varying"` is deliberately excluded even when
    `cardinality="single"`: SPEC §10.2/bitemporal.md allow multiple
    `active` assertions on the same time_varying predicate to coexist
    whenever their validity windows don't overlap (e.g. employment
    history) — declaring such a property `owl:FunctionalProperty` would
    assert a real OWL logical inconsistency (a reasoner given two
    literals on a functional datatype property reports the ontology
    unsatisfiable) for a state this codebase's own bitemporal model
    considers entirely valid. This export has no representation of valid
    time at all (see Consequences in ADR-0036) — every active assertion
    becomes one triple regardless of its validity window — so a
    time_varying property's cardinality genuinely isn't "at most one" in
    the exported graph, only "at most one **currently**, `as_of` the
    export moment" is what the KB itself would guarantee, and even that
    isn't expressible as a static OWL axiom.

    `required` is NOT encoded as an `owl:minCardinality` restriction —
    consistent with ADR-0028's core stance that `required` is
    validator-backed policy, not a structural constraint this codebase
    enforces at the core/schema layer.

    Every property/relation IRI referenced via `owl:inverseOf` is also
    declared its own `owl:ObjectProperty` type, even when the target
    concept doesn't independently declare that relation back (KI review
    finding: OWL 2 DL requires a declaration axiom for every IRI used as
    an object property; an undeclared `owl:inverseOf` target silently
    demotes the ontology to OWL 2 Full, which most reasoners reject).

    Args:
        schema: The `SchemaIR` to translate.

    Returns:
        An `rdflib.Graph` containing the OWL ontology. Callers wanting a
        serialized document call `graph.serialize(format=...)` on the
        result (e.g. `"turtle"`, `"xml"`, `"json-ld"` — any format rdflib
        supports).
    """
    graph = Graph()
    graph.bind("owl", OWL)
    graph.bind("rdfs", RDFS)
    graph.bind("xsd", XSD)

    ontology_iri = URIRef(base_iri(schema.namespace))
    graph.add((ontology_iri, RDF.type, OWL.Ontology))

    for concept in schema.concepts.values():
        class_iri = iri_for_concept(schema.namespace, concept.name)
        graph.add((class_iri, RDF.type, OWL.Class))
        graph.add((class_iri, RDFS.label, Literal(concept.name)))
        if concept.description:
            graph.add((class_iri, RDFS.comment, Literal(concept.description)))

        for prop in concept.properties.values():
            prop_iri = iri_for_property(schema.namespace, f"{concept.name}.{prop.name}")
            graph.add((prop_iri, RDF.type, OWL.DatatypeProperty))
            graph.add((prop_iri, RDFS.domain, class_iri))
            graph.add((prop_iri, RDFS.range, value_type_to_xsd(prop.value_type)))
            graph.add((prop_iri, RDFS.label, Literal(prop.name)))
            if prop.cardinality == "single" and prop.temporality == "static":
                graph.add((prop_iri, RDF.type, OWL.FunctionalProperty))
            if prop.description:
                graph.add((prop_iri, RDFS.comment, Literal(prop.description)))

        for rel in concept.relations.values():
            rel_iri = iri_for_property(schema.namespace, f"{concept.name}.{rel.name}")
            graph.add((rel_iri, RDF.type, OWL.ObjectProperty))
            graph.add((rel_iri, RDFS.domain, class_iri))
            graph.add((rel_iri, RDFS.range, iri_for_concept(schema.namespace, rel.target_concept)))
            graph.add((rel_iri, RDFS.label, Literal(rel.name)))
            if rel.cardinality == "single" and rel.temporality == "static":
                graph.add((rel_iri, RDF.type, OWL.FunctionalProperty))
            if rel.inverse:
                inverse_iri = iri_for_property(
                    schema.namespace, f"{rel.target_concept}.{rel.inverse}"
                )
                # OWL 2 DL requires a declaration for every object-property
                # IRI - the target concept isn't required to independently
                # declare this relation back, so declare it here too
                # (a no-op if it already is one, RDF graphs are sets).
                graph.add((inverse_iri, RDF.type, OWL.ObjectProperty))
                graph.add((rel_iri, OWL.inverseOf, inverse_iri))
            if rel.description:
                graph.add((rel_iri, RDFS.comment, Literal(rel.description)))

    return graph


__all__ = [
    "to_owl",
    "value_type_to_xsd",
    "base_iri",
    "iri_for_concept",
    "iri_for_property",
    "iri_for_entity",
]
