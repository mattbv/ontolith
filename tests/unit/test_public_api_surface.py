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

import subprocess
import sys

import ontolith
import ontolith.core
import ontolith.govern
import ontolith.identity
import ontolith.plugins
import ontolith.query
import ontolith.schema
import ontolith.store
import ontolith.store.duckdb
import ontolith.store.migrations
import ontolith.store.sqlite

_EXPECTED: dict[str, frozenset[str]] = {
    "ontolith": frozenset(
        {
            "Ontology",
            "AsOfView",
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
            "ObservabilitySink",
            "StdlibLoggingSink",
            "NullObservabilitySink",
            "RecordingObservabilitySink",
        }
    ),
    "ontolith.identity": frozenset(
        {
            "AdminAction",
            "AdminEvent",
            "AuthProvider",
            "Principal",
            "PrincipalCredential",
            "min_capability",
        }
    ),
    "ontolith.store": frozenset({"StorageBackend", "VECTOR_SCOPES", "DEFAULT_NAMESPACE"}),
    "ontolith.store.migrations": frozenset({"MigrationReport", "MigrationStep"}),
    "ontolith.store.sqlite": frozenset({"SQLiteBackend", "migrations"}),
    "ontolith.store.duckdb": frozenset({"DuckDBBackend", "migrations"}),
    "ontolith.govern": frozenset(
        {
            "Proposal",
            "ProposalEvent",
            "Provenance",
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
    "ontolith.store.migrations": ontolith.store.migrations,
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


def test_pinned_packages_import_cleanly_without_any_optional_extra() -> None:
    """Every pinned package must import with only the base (non-optional)
    dependency set installed — no package listed in _MODULES may eagerly
    pull in something gated behind `[project.optional-dependencies]`
    (`pyyaml`/`rdflib` behind `interop`, `duckdb` behind `store-duckdb`,
    etc.).

    Found during the M4 API-surface-freeze audit: `ontolith.schema`
    briefly re-exported `schema.linkml`'s `from_yaml`/`to_yaml` (a genuine
    gap — the interop extra's own module `__all__` already declared them
    public, the package `__init__.py` just never picked it up, the same
    shape as the `AsOfView`/`AuthProvider`/`VECTOR_SCOPES` gaps this same
    audit did fix) — verified broken (`ModuleNotFoundError: No module
    named 'yaml'` on plain `import ontolith.schema`) and reverted before
    merge, because `schema/linkml.py` imports `yaml` at module level and
    `pyyaml` is an `interop`-extra, not a base dependency. This test
    exists so that exact mistake, or its equivalent for `rdflib`/`duckdb`/
    any future optional dependency, fails CI automatically next time
    instead of depending on whoever's making the change noticing a
    same-shape docstring warning on a sibling module (which is what
    caught it this time, by luck of reading the neighboring file, not by
    any enforced check).

    Runs in a real subprocess (not in-process `sys.modules` poisoning) so
    a poisoned import can't leak into any other test in this process —
    `sys.modules['yaml'] = None` (etc.) makes Python's import system treat
    the module as confirmed-absent (raises `ModuleNotFoundError`
    immediately, no filesystem/site-packages lookup) without needing a
    real venv lacking those packages.
    """
    poisoned = ["yaml", "rdflib", "duckdb", "fastapi", "strawberry", "mcp"]
    script = (
        "import sys\n"
        f"for name in {poisoned!r}:\n"
        "    sys.modules[name] = None\n"
        "import ontolith\n"
        "import ontolith.core\n"
        "import ontolith.govern\n"
        "import ontolith.identity\n"
        "import ontolith.plugins\n"
        "import ontolith.query\n"
        "import ontolith.schema\n"
        "import ontolith.store\n"
        "import ontolith.store.migrations\n"
        "import ontolith.store.sqlite\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"A pinned package eagerly imports an optional dependency.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout
