"""Ontolith: A Python framework for collaborative knowledge bases.

Where humans and AI agents are co-equal authors of a shared, governed ontology.
"""

from ontolith.core import Assertion, Entity, FixedClock, SequentialIdProvider
from ontolith.identity import Principal
from ontolith.ontology import Ontology

__version__ = "0.0.1"

__all__ = [
    "Ontology",
    "Entity",
    "Assertion",
    "Principal",
    "FixedClock",
    "SequentialIdProvider",
]
