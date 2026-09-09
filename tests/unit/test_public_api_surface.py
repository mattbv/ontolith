"""Pins the public API surface defined in ADR-0019.

Fails if any package's `__all__` changes without a conscious update here —
the M3-level starting point for "public API stability policy begins"
(Implementation Plan §2). This catches export additions/removals/renames;
it does NOT catch signature-level changes to an already-exported symbol
(e.g. a new required parameter on `Ontology.propose`) — that gap is
tracked in KI-020, deferred pending a `griffe`-based CI diff gate.

If this test fails because of an intentional, reviewed change: update the
relevant entry in `_EXPECTED` below, and if anything was removed or
renamed, record it in CHANGELOG.md under a `**Breaking:**` marker per
ADR-0019's SemVer commitment.

`ontolith.interfaces.cli`/`ontolith.interfaces.mcp` are deliberately not
pinned here — ADR-0019 scopes this policy to the Python SDK surface, not
CLI flag or MCP tool-schema stability.
"""

import ontolith
import ontolith.core
import ontolith.govern
import ontolith.identity
import ontolith.plugins
import ontolith.query
import ontolith.schema
import ontolith.store
import ontolith.store.duckdb
import ontolith.store.sqlite

_EXPECTED: dict[str, frozenset[str]] = {
    "ontolith": frozenset(
        {
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
        }
    ),
    "ontolith.core": frozenset(
        {
            "Clock",
            "SystemClock",
            "FixedClock",
            "IdProvider",
            "UlidProvider",
            "SequentialIdProvider",
            "FixedIdProvider",
            "Entity",
            "Assertion",
            "AssertionEvent",
            "Namespace",
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
            "Embedder",
            "HashingEmbedder",
            "LookupEmbedder",
        }
    ),
    "ontolith.identity": frozenset(
        {"AdminAction", "AdminEvent", "Principal", "PrincipalCredential", "min_capability"}
    ),
    "ontolith.store": frozenset({"StorageBackend"}),
    "ontolith.store.sqlite": frozenset({"SQLiteBackend"}),
    "ontolith.store.duckdb": frozenset({"DuckDBBackend"}),
    "ontolith.govern": frozenset(
        {
            "Proposal",
            "ProposalEvent",
            "Contradiction",
            "Decision",
            "AutoAccept",
            "RequireReview",
            "Reject",
            "KbView",
            "PolicyStrategy",
            "ThresholdPolicy",
            "SourceQuorum",
            "ConfidenceThreshold",
            "SourceRequired",
            "RequireReviewByRole",
            "RequireReviewForAI",
            "Composite",
            "Activate",
            "Supersede",
            "Contradict",
            "ConflictResult",
            "route",
        }
    ),
    "ontolith.query": frozenset({"QueryBuilder"}),
    "ontolith.schema": frozenset(
        {
            "SchemaIR",
            "ConceptDef",
            "PropertyDef",
            "RelationDef",
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
            "compile_schema",
            "generate_class_stubs",
        }
    ),
    "ontolith.plugins": frozenset(
        {
            "PluginKind",
            "PluginCapabilities",
            "PluginManifest",
            "ReadOnlyView",
            "WriteView",
            "PluginRegistry",
            "LoadedPlugin",
            "Importer",
            "Exporter",
            "Reasoner",
            "Validator",
            "ValidatorKbView",
            "Connector",
        }
    ),
}

_MODULES = {
    "ontolith": ontolith,
    "ontolith.core": ontolith.core,
    "ontolith.identity": ontolith.identity,
    "ontolith.store": ontolith.store,
    "ontolith.store.sqlite": ontolith.store.sqlite,
    "ontolith.store.duckdb": ontolith.store.duckdb,
    "ontolith.govern": ontolith.govern,
    "ontolith.query": ontolith.query,
    "ontolith.schema": ontolith.schema,
    "ontolith.plugins": ontolith.plugins,
}


def test_pinned_packages_match_expected_snapshot() -> None:
    assert set(_MODULES) == set(_EXPECTED), (
        "A package was added to or removed from the pinned public API surface "
        "without updating both _MODULES and _EXPECTED (ADR-0019)."
    )


def test_public_api_surface_matches_pinned_snapshot() -> None:
    actual = {name: frozenset(mod.__all__) for name, mod in _MODULES.items()}
    assert actual == _EXPECTED, (
        "Public API surface changed (ADR-0019). If this is an intentional, "
        "reviewed change: update _EXPECTED above, and if anything was removed "
        "or renamed, record it in CHANGELOG.md under a '**Breaking:**' marker."
    )
