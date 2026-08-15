"""Required-fields validator reference plugin (SPEC §13, ADR-0015, KI-010).

Demonstrates the Validator protocol (plugins/ports.py): the kind of
domain rule a Validator plugin exists for — a business rule beyond
what the core schema enforces (Ontology.create_entity/propose take a
free-string concept/predicate with no required-predicate checking of
their own).

validate() is called per-assertion, so checking "does this entity have
all its required predicates" needs a sibling lookup: it resolves the
assertion's subject to an Entity (for its concept), then re-queries
kb.assertions(subject=...) for the full set of predicates currently
active on that subject. This whole-entity-completeness shape is why
Ontology only ever runs this validator (or any validator with the same
shape) via its `completeness_validators` constructor parameter, at
`accept_proposal` time — never per-assertion at direct-write time
(KI-042, ADR-0029): an entity built up one assertion at a time is, by
construction, incomplete after every write but its last.

PluginRegistry instantiates plugins with a no-argument constructor, so
`required_predicates` defaults to a small worked example (Person must
have a name) rather than an empty, silently-inert rule set — real
deployments would subclass, use `from_schema()`, or otherwise configure
their own rules.
"""

from collections.abc import Mapping, Sequence

from ontolith.core import Assertion
from ontolith.plugins.manifest import PluginManifest
from ontolith.plugins.ports import ValidatorKbView
from ontolith.schema import SchemaIR

_DEFAULT_REQUIRED_PREDICATES: Mapping[str, Sequence[str]] = {"Person": ("name",)}


class RequiredFieldsValidator:
    """Reference Validator plugin: flags entities missing required predicates."""

    manifest = PluginManifest(name="required-fields-validator", version="0.1.0", kind="validator")

    def __init__(self, required_predicates: Mapping[str, Sequence[str]] | None = None) -> None:
        self._required = (
            _DEFAULT_REQUIRED_PREDICATES if required_predicates is None else required_predicates
        )

    @classmethod
    def from_schema(cls, schema: SchemaIR) -> "RequiredFieldsValidator":
        """Derive `required_predicates` from a schema's declared `required` fields (KI-041).

        Scans every concept's properties and relations for `required=True`
        and collects their bare field names, so a schema author's
        `PropertyDef(required=True)`/`RelationDef(required=True)`
        declaration is what this validator enforces — instead of the
        separately hand-maintained default (or a caller-supplied mapping
        that can drift out of sync with the schema it's meant to mirror).

        Args:
            schema: Schema to derive required predicates from.

        Returns:
            A RequiredFieldsValidator configured from `schema`. A concept
            with no required properties/relations is simply absent from
            the resulting mapping (equivalent to an empty tuple).
        """
        required: dict[str, tuple[str, ...]] = {}
        for concept in schema.concepts.values():
            fields = sorted(
                [name for name, prop in concept.properties.items() if prop.required]
                + [name for name, rel in concept.relations.items() if rel.required]
            )
            if fields:
                # Keyed by concept.name, not the schema.concepts dict key -
                # validate() looks up self._required[entity.concept], and
                # entity.concept is set from ConceptDef.name, not from
                # whatever key a hand-built SchemaIR happened to store it
                # under (the class DSL/YAML front-ends always keep these
                # in sync, but a hand-built SchemaIR isn't required to).
                required[concept.name] = tuple(fields)
        return cls(required)

    def validate(self, assertion: Assertion, kb: ValidatorKbView) -> list[str]:
        """Return violation messages for missing required predicates, if any."""
        entity = kb.get_entity(assertion.subject)
        if entity is None:
            return [f"subject {assertion.subject!r} has no entity record"]

        required = self._required.get(entity.concept, ())
        if not required:
            return []

        # Predicates are qualified "Concept.field" (see QueryBuilder.where()'s
        # same convention) - required_predicates takes bare field names so
        # callers don't have to repeat the concept in every entry.
        present = {a.predicate for a in kb.assertions(subject=assertion.subject, status="active")}
        missing = sorted(field for field in required if f"{entity.concept}.{field}" not in present)
        label = entity.natural_key or entity.id
        return [
            f"{entity.concept} {label!r} missing required predicate {field!r}" for field in missing
        ]


__all__ = ["RequiredFieldsValidator"]
