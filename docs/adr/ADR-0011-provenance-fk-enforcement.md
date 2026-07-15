# ADR-0011: Provenance FK Enforcement at the Database Layer

**Status:** Accepted

**Date:** 2026-06-26

**Deciders:** Ontolith Core Team

**Related:** ADR-0003 (Agent Identity), ADR-0016 (DuckDB Backend), SPEC §7 (Assertions), SPEC §8 (Principals), SPEC §12.2 (SQLite schema)

---

## Context

SPEC §12.2 defines the normative SQLite schema for `entity` and `assertion` tables. Both tables carry provenance fields that reference principals by ID:

- `entity.created_by TEXT NOT NULL` — the principal who created the entity
- `assertion.author TEXT NOT NULL` — the principal who made the assertion

The SPEC normative schema defines these as plain `TEXT NOT NULL` columns without `FOREIGN KEY` constraints. However, SPEC §7 and §8 are explicit that `author` is a "principal id" and that every assertion must be attributable to a registered principal. ADR-0003 further enforces that AI principals must have an accountable owner — a guarantee that is meaningless if the principal record itself can be absent.

The question is: should DB-level FK constraints enforce what the SPEC mandates semantically?

## Decision

Add `FOREIGN KEY` constraints on both provenance fields:

```sql
CREATE TABLE entity (
  ...
  created_by TEXT NOT NULL,
  FOREIGN KEY(created_by) REFERENCES principal(id)
);

CREATE TABLE assertion (
  ...
  author TEXT NOT NULL,
  FOREIGN KEY(author) REFERENCES principal(id)
);
```

These constraints are enforced at runtime via `PRAGMA foreign_keys = ON`, which the `SQLiteBackend` sets on every connection.

## Rationale

**Why the SPEC's omission is not a prohibition:**

The SPEC normative schema is a minimal reference shape, not an exhaustive list of every constraint a conforming implementation should enforce. The SPEC mandates the provenance semantics; the FK is the mechanical expression of those semantics at the storage layer. Nothing in the SPEC, PRD, or any ADR argues against FK enforcement — the field was simply left unconstrained in the reference schema.

**Why application-layer validation alone is insufficient:**

Ontolith exposes multiple write paths: the Python SDK, the CLI, the future MCP server, and importer plugins. Any of these could — through a bug, a missing validation, or a partial migration — write an `author` that references a non-existent principal. Without a DB constraint, such writes succeed silently and corrupt the provenance trail. The FK is defense-in-depth: it catches what the application layer misses.

**Why this is load-bearing for the accountability model:**

ADR-0003 guarantees that every AI-authored assertion traces back to an accountable human owner. That guarantee only holds if the `author` principal record actually exists. A dangling `author` reference breaks the chain at the DB level in a way that is difficult to detect or repair after the fact.

**Why this is compatible with bulk import (LinkML/OWL/RDF):**

The `Importer` protocol (SPEC §13.2) writes through `WriteView` — the same governed API surface as the SDK. A correct importer registers principals before writing entities or assertions that reference them. This is standard ETL sequencing: principals first, then entities, then assertions. The FK enforces correct ordering rather than preventing it. Import jobs that represent an external source register a service principal for that source before ingesting its data.

**Why `PRAGMA foreign_keys = ON` is sufficient:**

SQLite's FK enforcement is per-connection and must be enabled explicitly. `SQLiteBackend.__init__` sets this pragma immediately after opening the connection, before any application code runs, so enforcement is reliable for all writes through this backend.

## Amendment (2026-07-14): DuckDB backend drops two non-provenance FKs

M3 added `DuckDBBackend` as a second `StorageBackend` implementation (ADR-0016). Its schema keeps every provenance FK this ADR mandates (`entity.created_by`, `assertion.author`, and the equivalent columns on `proposal`, `contradiction`, `principal_credential`) — the decision above is unaffected for provenance attribution.

Two *non-provenance* FKs were dropped, DuckDB-only: `assertion_event.assertion_id → assertion.id` and `proposal_event.proposal_id → proposal.id`. DuckDB 1.5.4 raises a false-positive constraint violation on `UPDATE ... RETURNING` against a row that is the target of an incoming FK from another table — confirmed empirically with a minimal repro — which breaks the existence-check pattern `set_assertion_status`/`update_proposal_state` rely on for status transitions. `SQLiteBackend` keeps both FKs; SQLite has no such interaction. This is a backend-specific workaround for a query-execution bug, not a reconsideration of this ADR's rationale: `assertion_event.assertion_id`/`proposal_event.proposal_id` reference rows created moments earlier in the same write path (never externally supplied like `author`/`created_by`), so the referential-integrity risk this ADR was written to close does not apply to them the same way. Referential integrity for these two columns is enforced at the application layer only, on the DuckDB backend, matching `Ontology`'s own write discipline. See `src/ontolith/store/duckdb/backend.py` (inline comments at the two `CREATE TABLE` statements) and KI-016 for the full empirical account.

## Consequences

**Positive:**
- ✅ Provenance integrity is enforced at the storage layer, not just the application layer
- ✅ Dangling author/created_by references are caught immediately, not silently stored
- ✅ ADR-0003's accountability guarantee (`owner IS NOT NULL` for AI principals) remains meaningful — the principal record must exist to be referenced
- ✅ Compatible with the import roadmap: importers must sequence principal creation before data ingestion, which is correct ETL practice

**Negative:**
- ⚠️ Test fixtures must pre-create a principal before creating entities or assertions — a minor setup cost
- ⚠️ Deviates from the §12.2 normative schema shape; a future SPEC revision should add these constraints

**Required test fixture pattern:**
```python
backend.put_principal(Principal(id="alice@example.com", kind="human", ...))
# now entity/assertion writes referencing alice@example.com are valid
```

## Alternatives Considered

**Follow the SPEC schema exactly (no FKs):**
- Rejected: Leaves provenance integrity as an application-only concern; any write path that skips validation silently corrupts the audit trail.

**Defer FKs to a later milestone:**
- Rejected: The cost of adding them now (fixture setup) is lower than retrofitting them after data is in the field. Corrupt provenance records are hard to detect and expensive to repair.

**Disable FKs during import and re-enable after:**
- Rejected: `PRAGMA foreign_keys` is a per-statement hint in SQLite, not a true disable/enable toggle across a transaction. Disabling it even temporarily creates a window for corrupt writes. Correct sequencing (principals before assertions) is a better invariant.

## References

- SPEC §7 (Assertions — `author` field definition)
- SPEC §8 (Principal & identity model)
- SPEC §12.2 (SQLite normative schema)
- SPEC §13.2 (Importer protocol / WriteView)
- ADR-0003 (Agent Identity — mandatory owner guarantee)
- ADR-0010 (SQLite transaction model — `PRAGMA foreign_keys = ON`)
