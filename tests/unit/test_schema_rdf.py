"""Unit tests for the RDF/OWL bridge's schema half (SPEC §13.3, ADR-0036)."""

from rdflib import OWL, RDF, RDFS, XSD, URIRef

from ontolith.schema import ConceptDef, PropertyDef, RelationDef, SchemaIR
from ontolith.schema.rdf import (
    base_iri,
    iri_for_concept,
    iri_for_entity,
    iri_for_property,
    to_owl,
    value_type_to_xsd,
)


class TestIriHelpers:
    def test_base_iri_is_urn_scoped_to_namespace(self) -> None:
        assert base_iri("default") == "urn:ontolith:default:"
        assert base_iri("other") == "urn:ontolith:other:"

    def test_iri_for_concept(self) -> None:
        assert iri_for_concept("default", "Person") == URIRef("urn:ontolith:default:class:Person")

    def test_iri_for_property_uses_dotted_predicate(self) -> None:
        assert iri_for_property("default", "Person.name") == URIRef(
            "urn:ontolith:default:property:Person.name"
        )

    def test_iri_for_entity(self) -> None:
        assert iri_for_entity("default", "e-1") == URIRef("urn:ontolith:default:entity:e-1")


class TestValueTypeToXsd:
    def test_maps_all_eight_value_types(self) -> None:
        assert value_type_to_xsd("Text") == XSD.string
        assert value_type_to_xsd("Integer") == XSD.integer
        assert value_type_to_xsd("Float") == XSD.double
        assert value_type_to_xsd("Boolean") == XSD.boolean
        assert value_type_to_xsd("Date") == XSD.date
        assert value_type_to_xsd("DateTime") == XSD.dateTime
        assert value_type_to_xsd("URI") == XSD.anyURI
        assert value_type_to_xsd("JSON") == XSD.string


class TestToOwl:
    def test_empty_schema_has_only_ontology_declaration(self) -> None:
        schema = SchemaIR(namespace="default", version=1, concepts={})
        graph = to_owl(schema)
        assert len(graph) == 1
        assert (URIRef(base_iri("default")), RDF.type, OWL.Ontology) in graph

    def test_concept_becomes_owl_class(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={"Person": ConceptDef(name="Person", description="A human")},
        )
        graph = to_owl(schema)
        class_iri = iri_for_concept("default", "Person")
        assert (class_iri, RDF.type, OWL.Class) in graph
        assert (class_iri, RDFS.label, None) in graph
        assert (class_iri, RDFS.comment, None) in graph

    def test_property_becomes_datatype_property_with_domain_and_range(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={"age": PropertyDef(name="age", value_type="Integer")},
                )
            },
        )
        graph = to_owl(schema)
        prop_iri = iri_for_property("default", "Person.age")
        class_iri = iri_for_concept("default", "Person")
        assert (prop_iri, RDF.type, OWL.DatatypeProperty) in graph
        assert (prop_iri, RDFS.domain, class_iri) in graph
        assert (prop_iri, RDFS.range, XSD.integer) in graph

    def test_property_description_becomes_rdfs_comment(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "age": PropertyDef(
                            name="age", value_type="Integer", description="Age in years"
                        )
                    },
                )
            },
        )
        graph = to_owl(schema)
        prop_iri = iri_for_property("default", "Person.age")
        assert (prop_iri, RDFS.comment, None) in graph

    def test_relation_description_becomes_rdfs_comment(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    relations={
                        "employer": RelationDef(
                            name="employer",
                            target_concept="Organization",
                            description="Current employer",
                        )
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )
        graph = to_owl(schema)
        rel_iri = iri_for_property("default", "Person.employer")
        assert (rel_iri, RDFS.comment, None) in graph

    def test_single_cardinality_property_is_functional(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text", cardinality="single")
                    },
                )
            },
        )
        graph = to_owl(schema)
        prop_iri = iri_for_property("default", "Person.name")
        assert (prop_iri, RDF.type, OWL.FunctionalProperty) in graph

    def test_many_cardinality_property_is_not_functional(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "phone": PropertyDef(name="phone", value_type="Text", cardinality="many")
                    },
                )
            },
        )
        graph = to_owl(schema)
        prop_iri = iri_for_property("default", "Person.phone")
        assert (prop_iri, RDF.type, OWL.FunctionalProperty) not in graph

    def test_relation_becomes_object_property_with_target_class_range(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    relations={
                        "employer": RelationDef(name="employer", target_concept="Organization")
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )
        graph = to_owl(schema)
        rel_iri = iri_for_property("default", "Person.employer")
        assert (rel_iri, RDF.type, OWL.ObjectProperty) in graph
        assert (rel_iri, RDFS.domain, iri_for_concept("default", "Person")) in graph
        assert (rel_iri, RDFS.range, iri_for_concept("default", "Organization")) in graph

    def test_relation_inverse_becomes_owl_inverse_of(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    relations={
                        "employer": RelationDef(
                            name="employer", target_concept="Organization", inverse="employees"
                        )
                    },
                ),
                "Organization": ConceptDef(
                    name="Organization",
                    relations={
                        "employees": RelationDef(
                            name="employees",
                            target_concept="Person",
                            cardinality="many",
                            inverse="employer",
                        )
                    },
                ),
            },
        )
        graph = to_owl(schema)
        employer_iri = iri_for_property("default", "Person.employer")
        employees_iri = iri_for_property("default", "Organization.employees")
        assert (employer_iri, OWL.inverseOf, employees_iri) in graph
        assert (employees_iri, OWL.inverseOf, employer_iri) in graph

    def test_relation_without_inverse_has_no_inverse_of_triple(self) -> None:
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    relations={
                        "employer": RelationDef(name="employer", target_concept="Organization")
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )
        graph = to_owl(schema)
        rel_iri = iri_for_property("default", "Person.employer")
        assert not any(graph.triples((rel_iri, OWL.inverseOf, None)))

    def test_output_serializes_and_reparses_as_valid_turtle(self) -> None:
        """Round-trip through rdflib's own parser - proves the graph is
        genuinely well-formed RDF, not just a collection of triples that
        happen to satisfy in-memory assertions."""
        from rdflib import Graph

        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={"name": PropertyDef(name="name", value_type="Text")},
                    relations={
                        "employer": RelationDef(name="employer", target_concept="Organization")
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )
        graph = to_owl(schema)
        serialized = graph.serialize(format="turtle")

        reparsed = Graph()
        reparsed.parse(data=serialized, format="turtle")
        assert len(reparsed) == len(graph)
