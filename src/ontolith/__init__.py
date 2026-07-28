"""Ontolith: A Python framework for collaborative knowledge bases.

Where humans and AI agents are co-equal authors of a shared, governed ontology.
"""

from ontolith.core import Assertion, Entity, FixedClock, Namespace, SequentialIdProvider
from ontolith.identity import Principal
from ontolith.ontology import Ontology
from ontolith.schema import (
    JSON,
    URI,
    Boolean,
    Concept,
    Date,
    DateTime,
    Float,
    Integer,
    Property,
    Ref,
    Relation,
    Text,
)

__version__ = "0.0.1"

__all__ = [
    "Ontology",
    "Entity",
    "Assertion",
    "Namespace",
    "Principal",
    "FixedClock",
    "SequentialIdProvider",
    "Concept",
    "Relation",
    "Property",
    "Ref",
    "Text",
    "Integer",
    "Float",
    "Boolean",
    "Date",
    "DateTime",
    "URI",
    "JSON",
]
