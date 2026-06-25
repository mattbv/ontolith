"""Error taxonomy for Ontolith.

Stable error hierarchy per SPEC §16 with machine-readable codes
that map consistently to HTTP/MCP status codes.
"""

from typing import Any


class OntolithError(Exception):
    """Base exception for all Ontolith errors.

    All errors carry:
    - A stable `code` for programmatic handling
    - A human-readable message
    - Optional structured `detail` for additional context
    """

    code: str = "ONTOLITH_ERROR"

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        """Initialize error with message and optional detail.

        Args:
            message: Human-readable error message.
            detail: Optional structured context (e.g., field names, constraints).
        """
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class SchemaError(OntolithError):
    """Invalid or incompatible schema or migration.

    Examples:
    - Breaking schema change without migration
    - Invalid concept/property definition
    - Schema version conflict
    """

    code: str = "SCHEMA_ERROR"


class ValidationError(OntolithError):
    """Validator or constraint failure.

    Carries a list of violations in the detail dict.

    Examples:
    - Required field missing
    - Type mismatch
    - Cardinality violation
    - Custom validator rejection
    """

    code: str = "VALIDATION_ERROR"


class AuthError(OntolithError):
    """Authentication failed or no identity resolved.

    Examples:
    - Invalid credentials
    - Expired token
    - Unknown principal
    """

    code: str = "AUTH_ERROR"


class CapabilityError(OntolithError):
    """Principal lacks required capability for operation.

    Examples:
    - Agent attempting direct write (requires 'write' capability)
    - User trying to review without 'review' capability
    - Missing admin capability for schema changes
    """

    code: str = "CAPABILITY_ERROR"


class PolicyDenied(OntolithError):
    """Proposal rejected by policy engine.

    The rejection reason is in the message and detail dict.

    Examples:
    - Confidence below auto-accept threshold
    - Missing required source
    - Trust level too low
    """

    code: str = "POLICY_DENIED"


class ConflictError(OntolithError):
    """Unresolved contradiction blocks an operation.

    Examples:
    - Query returns entities with flagged contradictions
    - Attempt to write when contradiction is open
    - Conflict resolution required before proceeding
    """

    code: str = "CONFLICT_ERROR"


class NotFoundError(OntolithError):
    """Entity, assertion, or namespace not found.

    Examples:
    - Entity ID doesn't exist
    - Namespace not initialized
    - Assertion ID invalid
    """

    code: str = "NOT_FOUND"


class StorageError(OntolithError):
    """Storage backend failure.

    Examples:
    - Database connection failed
    - Transaction timeout
    - Disk full
    - Backend-specific errors
    """

    code: str = "STORAGE_ERROR"


class PluginError(OntolithError):
    """Plugin load or execution failure.

    Examples:
    - Plugin not found
    - Plugin manifest invalid
    - Plugin raised exception
    - Sandbox violation
    """

    code: str = "PLUGIN_ERROR"


__all__ = [
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
