# ADR-0015: Plugin Capability Isolation

**Status:** Accepted

**Date:** 2026-07-09

**Deciders:** Ontolith Core Team

## Context

`src/ontolith/plugins/` contained only stale protocol stubs (`Embedder`/`PolicyStrategy`,
diverging from the real implementations those ports later got elsewhere) — no registry, no
manifest schema, no entry-point discovery, and no capability sandbox.

SPEC §13.1: "Plugins are discovered via Python entry points under the `ontolith.plugins` group.
Each plugin **MUST** declare a `name`, `version`, and a `capabilities` manifest. The runtime
**MUST** load plugins with least privilege (§17) and **MUST** allow disabling any plugin per
namespace." SPEC §13.2: "Reasoner-derived assertions **MUST** enter through the proposal path
(§9); plugins **MUST NOT** bypass governance." SPEC §17: "**Plugin sandboxing**: third-party
plugins run with a declared capability manifest; the runtime **SHOULD** isolate execution and
**MUST** deny undeclared access (storage, network, filesystem)." None of this was enforced
anywhere because nothing loaded a plugin.

This gap was surfaced during the 2026-07-08 post-remediation security re-audit and tracked as
`docs/known-issues.md` KI-014: a hypothetical in-process plugin, once loading exists, would run
fully trusted and could call `backend.put_assertion` directly (bypassing governance) or
`Ontology.issue_token` (now admin-gated per PR #17/ADR-0014) to mint itself a high-capability MCP
credential. Not exploitable today — no plugin loader existed — but the isolation model needs to
exist *before*, not after, plugin discovery is turned on.

## Decision

**A plugin is registered as a `service`-kind `Principal` with a capped `default_capability`. The
sandbox is a capability-scoped facade over `Ontology` (`ReadOnlyView`/`WriteView`), not a parallel
authorization system.**

- **Manifest** (`plugins/manifest.py`): `PluginManifest(name, version, kind, capabilities)`.
  `kind` is one of `importer|exporter|reasoner|validator|embedder|connector`.
  `PluginCapabilities.storage` (`read|propose|write`) is **enforced**. `network`/`filesystem`
  (bool) are **declared but not enforced** in this pass — see Consequences.
- **Plugin kinds in scope**: the six kinds above only. `StorageBackend`, `AuthProvider`, and
  `PolicyStrategy` plugins are deliberately excluded — they are infrastructure-extension points
  the framework calls *into* (a `StorageBackend` plugin **is** the persistence substrate
  underneath `WriteView`, not a principal-scoped actor calling through it; `AuthProvider.resolve`
  runs *before* any Principal/view exists; `PolicyStrategy.evaluate` is pure logic `propose()`
  itself invokes). None fit the capability-scoped-view model. `AuthProvider`/`PolicyStrategy`
  already have real implementations (`identity/ports.py`, `govern/policy.py`); this ADR doesn't
  touch them.
- **Views** (`plugins/views.py`): `ReadOnlyView` exposes `get_entity`/`assertions`/`query`/`as_of`
  — already-open reads, pure surface restriction. `WriteView` (extends `ReadOnlyView`) adds
  `create_entity`/`propose`/`propose_ref`/`retract`, each hardcoding `author` to the view's own
  bound principal id. Admin-only methods (`issue_token`, `apply_schema`, `create_principal`,
  `accept_proposal`, `reject_proposal`, `resolve_contradiction`, `flag_contradiction`) and the
  direct-write bypass (`assert_literal`, `assert_ref`) are **structurally absent** — not
  runtime-checked. No view method takes `acting_as`: delegation is a human/AI-owner concept
  (ADR-0003), and letting a plugin act as another principal would reopen the spoofing class of bug
  ADR-0014 closed.
- **Registry** (`plugins/registry.py`): `PluginRegistry.register(entry_point_name, *, author,
  granted_capability="propose")` requires `author` to hold `admin` capability (mirrors
  `Ontology.issue_token`'s gate — `Ontology._require_admin` renamed to `require_admin`, its first
  legitimate caller outside the class). Discovers via
  `importlib.metadata.entry_points(group="ontolith.plugins")`. Effective capability =
  `min_capability(manifest.capabilities.storage, granted_capability)`, then **hard-forced to
  `"read"`** for read-only kinds (`exporter`/`validator`/`embedder`) regardless of what the
  manifest requests or the operator grants — an override, not another `min()`, since there's no
  lattice level below `"read"`. Creates (or reuses, with a kind/capability consistency check) a
  `service`-kind Principal (`auth_method="workload"`) named after the manifest, then builds the
  appropriate view.
- **`create_entity` capability gate**: `Ontology.create_entity` previously had no capability check
  at all — any author string, even a nonexistent one, could create entities. Fixed alongside this
  ADR (require `>= propose`, matching `flag_contradiction`'s existing gate) so `WriteView` could
  expose entity creation meaningfully to Importer/Connector plugins without inheriting a
  pre-existing unrelated gap.

## Rationale

**Why reuse the Principal/capability lattice instead of a parallel plugin-ACL system:** This is
the core insight — the sandbox is a *view*, not a new authorization mechanism. `min_capability`
(`identity/principal.py`) and `Ontology.propose`/`propose_ref`/`retract` already do exactly the
capability checking, conflict routing, and provenance work a plugin's writes need; reimplementing
that at a lower layer (e.g., wrapping `StorageBackend` directly) would duplicate logic and risk it
drifting out of sync with the real governance path.

**Why structural absence instead of runtime blocking:** A method that isn't on the view class
can't be called by accident, can't be forgotten when a new admin method is added later, and is
checkable by reading the class definition rather than by testing every call site for a missing
`if`. This is stronger than an allow-list check that could be deleted or bypassed by a future
edit.

**Why `service`-kind + `auth_method="workload"` rather than a new `Principal.kind`:** A plugin is
not human (`oidc`) and isn't yet authenticating via a bearer token the way MCP callers do
(`apikey`) — it's a workload identity resolved through entry-point discovery and admin
registration. `"workload"` was already a `Principal.auth_method` literal (ADR-0014 named it as
future non-human auth); this reuses it rather than adding a fourth `kind`.

## Consequences

**Positive:**
- ✅ Closes both named audit findings *structurally*, for any plugin using the intended API
  surface: `backend.put_assertion` is unreachable (`WriteView` never touches `backend`, only
  `Ontology`'s governed methods), and `issue_token` self-minting is unreachable (not a method on
  any view).
- ✅ Least privilege is the default and the ceiling: `granted_capability` defaults to `"propose"`,
  and read-only plugin kinds are hard-capped at `"read"` even if their manifest or an operator's
  grant asks for more.
- ✅ Reuses 100% of existing governance machinery — no new capability lattice, no new
  policy-evaluation path.

**Negative:**
- ⚠️ **Network/filesystem are declared but not enforced.** A plugin can still `import requests` or
  `open()` directly — this is in-process Python with no process/wasm sandboxing yet.
  **Mitigation:** matches `docs/Ontolith_Implementation_Plan.md`'s own phasing exactly: "Plugin
  sandboxing: capability manifests, deny-by-default for storage/network/filesystem; **process
  isolation in v1, wasm/subprocess hardening + signed registry plugins by 1.0**." `register()`
  requiring `admin` capability means only an already-trusted operator can load a plugin at all
  today (no self-service/marketplace loading exists), bounding exposure until process isolation
  lands. KI-014 stays open for this half.
- ⚠️ **This is a governance-correctness boundary, not a security sandbox against hostile code.**
  Python has no true encapsulation — a deliberately malicious plugin can reach past a view's
  private `_kb` reference (`view._kb.issue_token(...)`), and the same applies to any object a view
  legitimately returns that itself holds a raw backend reference (e.g. `view.query(...)`'s
  `QueryBuilder`) — nothing in this design stops either path. This design closes the
  *structural/accidental* bypass (the two specific findings named in the audit) for any plugin
  using its view normally. It does **not** close a plugin author who deliberately writes code to
  defeat the convention. That gap closes only with process/wasm isolation, already correctly
  deferred per the phasing quoted above. **This is more consequential than the network/filesystem
  gap and must not be mistaken for a complete fix of KI-014's threat model** —
  it closes the "trusted-but-careless" and accidental-bypass paths, not deliberate malice.
- ⚠️ No per-namespace plugin enable/disable (SPEC §13.1 MUST) — the project is still
  single-namespace throughout (`Ontology.__init__`'s existing scope limitation), so this doesn't
  apply yet either; not a regression introduced here.

## Alternatives Considered

**A capability-restricted `StorageBackend` wrapper per plugin:** This was KI-014's own original
fix sketch. Rejected in favor of the view-based approach — a `StorageBackend` wrapper would have
to reimplement policy evaluation, conflict routing, and model-requirement checks a second time at
a lower layer (duplicating what `Ontology.propose` already does correctly), and it would *not*
naturally block `issue_token`/`apply_schema`/`create_principal` since none of those are
`StorageBackend` methods anyway — a view over `Ontology` blocks them for free, by omission,
without a wrapper needing to know about them explicitly.

**A new, parallel plugin-specific capability system:** Rejected — the existing
`read<propose<write<review<admin` lattice already fits the storage-capability axis a plugin needs;
inventing a second one would be pure duplication for no expressiveness gain.

**Process or subprocess isolation now:** Rejected as premature for this pass — a much larger
infrastructure commitment than the storage-capability view, and the Implementation Plan already
correctly phases it to "v1... hardened... by 1.0." Building it now, before any real plugin exists
to run in it, would be speculative infrastructure ahead of need.

## References

- `docs/known-issues.md` KI-014 (the finding this ADR closes, storage half)
- ADR-0003 (Agent identity, `acting_as` delegation — the reason views expose no delegation surface)
- ADR-0014 (MCP authentication — the `require_admin`/token-issuance-gate precedent this mirrors)
- SPEC §13 (Plugin protocols and discovery), §17 (Security model, plugin sandboxing)
- `docs/Ontolith_Implementation_Plan.md` (plugin sandboxing phasing: process isolation in v1,
  wasm/subprocess hardening by 1.0)
