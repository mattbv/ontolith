"""Core abstractions and ports for Ontolith.

This module contains the fundamental building blocks:
- Clock and IdProvider ports for deterministic behavior
- Error taxonomy for stable error handling
- Future: meta-model, IR, validation
"""

from ontolith.core.clock import Clock, FixedClock, SystemClock
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
