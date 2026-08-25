# ADR-0034: `ontolith schema migrate` Is a Thin YAML-File Wrapper Around `apply_schema`; Data Migration Is Explicitly Out of Scope

**Status**: Accepted
**Date**: 2026-08-25
**Deciders**: Ontolith Core Team
**Related**: SPEC §14.2 (`ontolith schema {show|migrate}`), SPEC §6.1 (one IR, two front-ends), ADR-0013 (LinkML dialect coverage), KI-038 (`schema show`), KI-048

---

## Context

SPEC §14.2 names `ontolith schema {show|migrate}` as the CLI's normative schema surface. KI-038 implemented `show`; `migrate` was explicitly left unimplemented and forward-tracked as KI-048, since "migrate" is ambiguous without a scope decision: schema versioning already exists (`Ontology.apply_schema` enforces strict monotonic version numbering) but nothing in the codebase does anything migration-shaped to *data* — no diffing between schema versions, no backfill, no property-rename/retype handling against already-stored assertions, on any interface (SDK, REST, MCP, CLI). KI-048's own Fix text names two possible scopes and asks that the boundary be recorded as an ADR rather than decided implicitly inside a CLI PR:

- **(a)** A thin CLI wrapper around the already-governed `apply_schema` — read a schema document from disk, apply it as a new version. No new domain logic.
- **(b)** Something that also handles property renames/type changes against existing assertion data — a substantially larger scope. Renaming a predicate doesn't rewrite historical assertions (append-only), so old and new predicate names would coexist under any such mechanism; this touches append-only semantics directly and needs its own design work, not a CLI command's implicit choice.

KI-048's Fix text already recommends scoping to (a) first, so this ADR does not reopen that choice — it records it, and adds one further scope decision (a) itself didn't resolve: which schema *format* the CLI reads from disk.

## Decision

**Scope: (a) only.** `ontolith schema migrate <file> --author <admin>` reads a file from disk, parses it into a `SchemaIR`, and calls `Ontology.apply_schema(schema, author=author)` — the same governed, capability-checked path every other schema-applying caller (SDK, tests) already uses. No new `StorageBackend` port method, no new domain logic in `core`/`govern`/`schema`. `apply_schema`'s existing strict-monotonic version check is unchanged and unbypassed: the file must already declare the correct next version (`current_latest + 1`, or `1` if none exists), the same requirement any other `apply_schema` caller already has.

**Format: LinkML-aligned YAML only, not class-DSL.** The file is parsed via `schema.linkml.from_yaml` (ADR-0013's v1 dialect) — a plain-data format that can be read and parsed safely from an arbitrary caller-supplied path. Class-DSL schemas (`ontolith.schema.dsl.Concept` subclasses, `compile_schema()`) are Python source: there is no existing mechanism anywhere in this codebase to load a `SchemaIR` from a class-DSL *file path* — `compile_schema()` takes already-imported Python classes, not a file to read. Building one would mean dynamically importing and executing arbitrary user-supplied Python from a CLI argument, a materially different and riskier capability than parsing structured data, and not something SPEC §14.2 or KI-048 asked for. A user with a class-DSL schema already has the natural path: import it and call `Ontology.apply_schema()` directly from Python, or `schema.dsl.compile_schema(...)` → `schema.linkml.to_yaml(...)` to produce a YAML file this command can consume — both existing, no new code needed.

**Data migration/backfill (scope (b)) remains explicitly out of scope**, tracked as its own future decision, not folded into this command. `apply_schema` applying a new schema version is purely additive to the schema's own version history; it has never rewritten or validated existing assertion data against the new version (this predates KI-048 and is unchanged by it — see KI-049 for the related, also-open gap that write-time `value_type` isn't even validated against literal content, let alone retroactively).

## Rationale

**Why not add file-existence/format validation beyond what `from_yaml`/`apply_schema` already raise:** every other CLI command in `interfaces/cli.py` funnels domain errors through one `except Exception as exc: typer.echo(f"Error: {exc}", ...)` handler rather than special-casing validation per command. `file.read_text()` raising `FileNotFoundError` for a missing path, `from_yaml` raising `SchemaError` for a malformed document, and `apply_schema` raising `SchemaError`/`CapabilityError`/`AuthError` for a version/capability mismatch all already produce a reasonable message through that same generic path — adding command-specific pre-validation would duplicate error handling this codebase deliberately keeps uniform.

**Why the file argument doesn't infer or auto-increment `version`:** `from_yaml` already requires an explicit integer `version` in the document (ADR-0013), and `apply_schema` already enforces it's the exact next version — silently rewriting the file's declared version to "whatever's next" would hide a real mismatch (e.g. a schema file written against a stale version this call would then silently paper over) rather than surfacing it as the same `SchemaError` a programmatic `apply_schema` caller already gets for the identical mistake.

## Consequences

**Positive:**
- Closes the `migrate` half of KI-038/SPEC §14.2's CLI surface with no new domain logic — `apply_schema` already does all the governed work.
- A class-DSL schema still reaches this command via the existing `to_yaml` round-trip; no capability is lost, only the direct file-path shortcut for that one format.

**Negative / follow-ups:**
- **Not a "migration" in the general sense** — no diffing, no backfill, no handling for a property's `value_type` or `temporality` changing between versions against data already written under the prior version. A deployment expecting this command to reconcile existing data will be surprised; the command's own docstring and this ADR say so explicitly.
- Scope (b) — actual data-affecting migration — remains unscoped. If picked up later, it needs its own ADR given the append-only implications KI-048's Fix text already flagged (a renamed/retyped predicate can't rewrite history; old and new predicate names would need to coexist under some reconciliation strategy yet to be designed).
- No conformance vector — this is a CLI-only wrapper with no new backend/domain behavior, so it's covered by a CLI unit test (`tests/unit/test_cli.py::TestSchemaMigrate`) rather than the conformance kit, matching `schema show`'s own precedent (KI-038).
- **The YAML file must declare the complete schema, not a delta** — `apply_schema` replaces the namespace's active `SchemaIR` wholesale (`put_schema`, no merge with the prior version), a pre-existing property of `apply_schema` itself, not something this command changes. But "migrate" invites reading this as "apply an incremental change," and the CLI now puts that misreading one command away — a document that omits a concept the prior version declared silently makes that concept's predicates unknown to schema-validated writes going forward. The command's own docstring states this explicitly (found during this ADR's own review).

## Alternatives Considered

- **Support both YAML and class-DSL file input** (full scope (a) as KI-048's Fix text phrased it): rejected for the reasons in Decision above — no existing mechanism loads a `SchemaIR` from a class-DSL file path without dynamically executing arbitrary Python, a materially larger and riskier feature than this ADR's scope.
- **Auto-derive the next version instead of requiring it in the file**: rejected — see Rationale; would hide real version mismatches instead of surfacing them.
- **Bundle scope (b) now instead of deferring it**: rejected — KI-048's own Fix text already flags this as a substantially larger, append-only-semantics-touching design question that shouldn't be decided implicitly inside a CLI command's PR.
