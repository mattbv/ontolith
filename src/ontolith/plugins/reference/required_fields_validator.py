"""Required-fields validator reference plugin (SPEC §13, ADR-0015, KI-010).

Demonstrates the Validator protocol (plugins/ports.py): the kind of
domain rule a Validator plugin exists for — a business rule beyond
what the core schema enforces (Ontology.create_entity/propose take a
free-string concept/predicate with no required-predicate checking of
their own).

validate() is called per-assertion, so checking "does this entity have
all its required predicates" needs a sibling lookup: it resolves the
assertion's subject to an Entity (for its concept), then re-queries
ReadOnlyView.assertions(subject=...) for the full set of predicates
currently active on that subject.

PluginRegistry instantiates plugins with a no-argument constructor, so
`required_predicates` defaults to a small worked example (Person must
have a name) rather than an empty, silently-inert rule set — real
deployments would subclass or otherwise configure their own rules.
"""

from collections.abc import Mapping, Sequence

from ontolith.core import Assertion
from ontolith.plugins.manifest import PluginManifest
from ontolith.plugins.views import ReadOnlyView

_DEFAULT_REQUIRED_PREDICATES: Mapping[str, Sequence[str]] = {"Person": ("name",)}


class RequiredFieldsValidator:
    """Reference Validator plugin: flags entities missing required predicates."""

    manifest = PluginManifest(name="required-fields-validator", version="0.1.0", kind="validator")

    def __init__(self, required_predicates: Mapping[str, Sequence[str]] | None = None) -> None:
        self._required = (
            _DEFAULT_REQUIRED_PREDICATES if required_predicates is None else required_predicates
        )

    def validate(self, assertion: Assertion, kb: ReadOnlyView) -> list[str]:
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
