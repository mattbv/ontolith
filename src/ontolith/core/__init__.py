"""Core abstractions and ports for Ontolith.

This module contains the fundamental building blocks:
- Clock and IdProvider ports for deterministic behavior
- Embedder port for hybrid retrieval (SPEC §11.3)
- ObservabilitySink port for metrics/events/structured logs (SPEC §18, ADR-0044)
- Error taxonomy for stable error handling
- Entity and Assertion value objects

The meta-model/IR/validation this docstring once listed as "Future" now
lives in `ontolith.schema` (`SchemaIR`, `ConceptDef`/`PropertyDef`/
`RelationDef`, the class DSL) — not `ontolith.core`, and shipped in M1/M3.
"""

from ontolith.core.assertion import Assertion, AssertionEvent
from ontolith.core.clock import Clock, FixedClock, SystemClock
from ontolith.core.embedder import Embedder, HashingEmbedder, LookupEmbedder
from ontolith.core.entity import Entity
from ontolith.core.errors import (
    AuthError,
    CapabilityError,
    ConflictError,
    NotFoundError,
    OntolithError,
    PluginError,
    PolicyDenied,
    SchemaError,
    StorageError,
    ValidationError,
)
from ontolith.core.ids import (
    FixedIdProvider,
    IdProvider,
    SequentialIdProvider,
    UlidProvider,
)
from ontolith.core.namespace import Namespace
from ontolith.core.observability import (
    NullObservabilitySink,
    ObservabilitySink,
    RecordingObservabilitySink,
    StdlibLoggingSink,
)

__all__ = [
    # Clock
    "Clock",
    "SystemClock",
    "FixedClock",
    # IDs
    "IdProvider",
    "UlidProvider",
    "SequentialIdProvider",
    "FixedIdProvider",
    # Embedder
    "Embedder",
    "HashingEmbedder",
    "LookupEmbedder",
    # Observability
    "ObservabilitySink",
    "StdlibLoggingSink",
    "NullObservabilitySink",
    "RecordingObservabilitySink",
    # Domain models
    "Entity",
    "Assertion",
    "AssertionEvent",
    "Namespace",
    # Errors
    "OntolithError",
    "SchemaError",
    "ValidationError",
    "AuthError",
    "CapabilityError",
    "PolicyDenied",
    "ConflictError",
    "NotFoundError",
    "StorageError",
    "PluginError",
]
