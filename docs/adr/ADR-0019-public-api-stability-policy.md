# ADR-0019: Public API Stability Policy (M3 Exit Criterion Begins)

**Status**: Accepted
**Date**: 2026-07-15
**Deciders**: Ontolith Core Team
**Related**: Implementation Plan §2 (M3 exit criteria — "public API stability policy begins"), §5 (quality gates — `griffe diff`), §7.2 (SemVer commitment), §14 open question 6 ("exactly which symbols are covered by the SemVer guarantee")

---

## Context

The Implementation Plan names "public API stability policy begins" as one of M3's three exit criteria, alongside "backend conformance kit passes on a 2nd backend" (closed by ADR-0016/KI-016) and "plugin contract tests green" (closed by the plugin contract kit, `conformance/test_plugin_contract.py`). Unlike those two, nothing had been decided yet: the Implementation Plan's own open-questions list (§14.6) asks "exactly which symbols are covered by the SemVer guarantee at 1.0" without answering it, and §7.2 states the SemVer commitment ("pre-1.0: minor versions may break, documented in CHANGELOG; post-1.0: breaking changes only on majors") without saying what "the API" means concretely — every module, or a defined subset.

§5's quality-gates table separately names a `griffe diff` gate ("no unintended breaking change... warn pre-1.0, block post-1.0"). Investigating this: `griffe`'s CLI has been split into a separate `griffecli` distribution in the installed version (`griffelib` 2.1.0, pulled in transitively via `mkdocstrings[python]`), which is not currently a project dependency — `python -m griffe` fails with `ModuleNotFoundError: griffecli`. Standing up automated API-diff CI would mean adding and pinning a new tool dependency now, ahead of the `security.yml`/`nightly.yml` CI workflows (`bandit`, `gitleaks`, `pip-audit`, SBOM) that Implementation Plan §7.1 describes alongside it — none of which exist yet either. Building one piece of that CI-hardening pass in isolation, without the surrounding infrastructure it's meant to sit next to, risks committing to tooling choices that should be made together.

Meanwhile, every public package already ships a curated `__all__` (`ontolith`, `ontolith.core`, `ontolith.identity`, `ontolith.store`, `ontolith.store.sqlite`, `ontolith.store.duckdb`, `ontolith.govern`, `ontolith.query`, `ontolith.schema`, `ontolith.plugins`) — the scoping work of "what's exported" is already done in code; what's missing is a written commitment about what that means and a way to catch accidental drift.

## Decision

**1. Define the public API surface** as the union of every `__all__` list in the packages above, plus the documented public (non-underscore-prefixed) methods and attributes of the classes those `__all__` lists export — most centrally `Ontology` (the SDK's primary entry point) and `ReadOnlyView`/`WriteView` (the plugin-facing entry points). Everything not reachable through one of those paths — private (`_`-prefixed) names, internal helper modules, and test/conformance/benchmark code — is not covered.

**Explicitly excluded from this policy**: `ontolith.interfaces.cli` and `ontolith.interfaces.mcp`. Both have their own `__all__`, but their compatibility contract is different in kind — CLI argument/flag stability and MCP tool-schema stability, not Python import/call-signature stability — and mixing the two would blur what "breaking" means for each. If a CLI/MCP stability policy is written later, it should be its own ADR.

**`ontolith.interfaces.rest` and `ontolith.interfaces.graphql` ARE included** (resolved by the 2026-09-21 Update below, round 1 of its own review — an earlier version of this ADR left both unaddressed): `create_rest_app`/`create_graphql_app` are genuine Python functions with a genuine Python call signature, not a CLI-flag or MCP-tool-schema kind of contract — squarely what this policy is about, unlike `interfaces.cli`/`interfaces.mcp` above. Pinned the same way `ontolith.store.duckdb` already is: part of the SemVer-covered, `__all__`-pinned surface, but exempt from the "importable with zero optional extras" guarantee, since both require their own extra (`rest`/`graphql`) to import at all (`fastapi`/`strawberry-graphql` at module level).

**2. Confirm the SemVer commitment already stated in Implementation Plan §7.2**: pre-1.0, minor version bumps may break the public API surface defined above, but every such break **MUST** be recorded in `CHANGELOG.md` under a `**Breaking:**` marker — a practice already in use (see the 2026-07-06 remediation entries) that this ADR now makes a stated requirement rather than an informal convention. Post-1.0, breaking changes require a major version bump; that line isn't reached yet and isn't re-litigated here.

**3. Enforce the surface now, without new tooling**: add `tests/unit/test_public_api_surface.py`, a regression test pinning the exact `__all__` of every package named above. Changing any package's public exports requires touching this test alongside the change — a cheap, immediate, in-repo tripwire against silent drift, satisfying the "begins" framing of the M3 exit criterion without prematurely standing up `griffe`-based CI ahead of the rest of the security/release CI pass it belongs with.

**4. Defer the automated `griffe diff` CI gate** from §5's quality-gates table to whenever the `security.yml`/`nightly.yml`/`release.yml` workflows described in §7.1 are built — tracked as a known gap below, not silently dropped.

## Rationale

**Why define scope via existing `__all__` lists instead of a hand-maintained allowlist**: every package already curates one deliberately (confirmed by reading all ten `__init__.py` files) — inventing a second, parallel list to serve as "the real public API" would immediately drift from the first and double the maintenance burden for no signal gained.

**Why a pinned-surface test now instead of `griffe` now**: a static list comparison catches the exact failure mode this ADR exists to prevent — an export silently added, renamed, or removed — with zero new dependencies and zero CI-pipeline design work. `griffe` additionally catches signature-level changes (a parameter added/removed/retyped on an already-exported symbol), which the pinned-list test does not; that's a real gap, explicitly left open rather than papered over (see Consequences).

**Why exclude `interfaces/cli`/`interfaces/mcp`**: `Ontology.propose()` changing its signature and `ontolith propose` changing its CLI flags are different kinds of breakage with different audiences and different tooling to detect them (Python signature diffing vs. CLI/MCP contract testing). SPEC §16's error-taxonomy stability and the MCP tool-schema tests (`interfaces/mcp.py`'s own test suite) already partially cover the MCP side; folding both into one "public API" definition would understate what's actually being promised for each.

## Consequences

**Positive:**
- M3's third exit criterion has a concrete, checked-in artifact (`tests/unit/test_public_api_surface.py`) rather than remaining an unanswered open question.
- Answers Implementation Plan §14.6 ("exactly which symbols are covered") in writing, for the first time.
- Formalizes an already-informally-followed CHANGELOG practice instead of leaving it to convention.

**Negative / follow-ups:**
- The pinned-`__all__` test does not catch signature-level breakage (e.g. a required parameter added to `Ontology.propose()` without changing `__all__`) — only export-set changes. This is a real, accepted gap until the `griffe` gate lands.
- The `griffe diff` CI gate from Implementation Plan §5 remains unimplemented; tracked in `docs/known-issues.md` (new entry) rather than silently dropped, to be picked up alongside the broader `security.yml`/CI-hardening pass.
- `interfaces/cli`/`interfaces/mcp` stability is explicitly out of scope here and remains undocumented as its own policy — a gap, not a decision that it doesn't matter.

## Alternatives Considered

**Stand up `griffe`-based CI now, add `griffecli` as a new dev dependency**: Rejected for this pass — `griffe`'s CLI packaging is mid-transition (the installed `griffelib` 2.1.0 already requires a separate `griffecli` install for `python -m griffe` to work), and the diff-gate is naturally one piece of a CI-hardening pass (`bandit`, `gitleaks`, `pip-audit`, SBOM, `griffe`) that doesn't exist yet in any form — building this one piece in isolation risks a shape that doesn't fit the rest once that pass happens.

**Treat every symbol reachable via `import ontolith` (including private-looking internals accidentally importable) as "public"**: Rejected — this is effectively "no policy," since it can't be enforced without also freezing implementation details that were never meant to be part of the contract (e.g. `Ontology._resolve_temporality`). Scoping to `__all__` + documented public methods is the only version of "public" that's actually decidable from the code.

**Do nothing until `griffe` tooling is ready, leave the exit criterion unmet**: Rejected — "begins" is the actual M3 bar per the Implementation Plan, not "fully automated." A written policy plus a cheap regression test is a genuine start; waiting on a specific tool's packaging to stabilize before writing anything down was the status quo this ADR replaces.

## Update (2026-09-21): M4 audit + 1.0 freeze declaration, closing Implementation Plan §14's open question 6

**KI-020 (the deferred `griffe diff` CI gate named in Consequences/Alternatives above) has already
been resolved since M3, via ADR-0026** — noted here because this Update relies on that gate existing,
not because this branch resolves it. `.github/workflows/ci.yml`'s `quality` job runs `griffe check`
against the PR's base commit on every PR, reporting via native GitHub Actions annotations —
`continue-on-error: true` keeps it informational pre-1.0, exactly as this ADR's own Decision #4
anticipated. Flipping it to blocking is a one-line removal of that flag, done at the actual 1.0 tag
(see below), not now — see KI-020's own resolution note (and ADR-0026) for the full record.

**M4 Workstream 5 (Implementation Plan §14's open question 6 — "SemVer 1.0 API freeze") audited the
full surface this ADR's Decision #1 defines, before declaring it the 1.0 baseline.** The audit walked
every package named in Decision #1, checking for a class/constant/function defined in a public
module with its own `__all__` that the *package*-level `__init__.py` never picked up — the exact
shape a later addition can silently introduce, since nothing enforces "every module-level `__all__`
entry reaches the package" the way `test_public_api_surface.py` enforces "the package's own
`__all__` doesn't drift." Found and fixed three real gaps, each verified by direct import (not just
inspection) before and after:

- **`AsOfView`** (`ontology.py`, the return type of `Ontology.as_of()` and `ReadOnlyView.as_of()`,
  structurally identical to `QueryBuilder` — a live-backend-holding view class — which *was* already
  exported) was reachable only via `kb.as_of(t)`'s return value, never importable directly for type
  annotation. `ontology.py`'s own module-level `__all__` already listed it (`["AsOfView",
  "Ontology"]`) — the package `__init__.py` just never re-exported it. Now exported from `ontolith`
  alongside `Ontology`.
- **`AuthProvider`** (`identity/ports.py`, the abstract port `create_rest_app`/`create_graphql_app`/
  `create_mcp_server` all take, and whose own docstring is the canonical description of the
  interface a custom auth backend implements) had no import path shorter than
  `ontolith.identity.ports`. Now exported from `ontolith.identity`. (`TokenAuthProvider`/`hash_token`
  were not exported at this point in the audit — round 1's review found they should be; see below.)
- **`VECTOR_SCOPES`/`DEFAULT_NAMESPACE`** (`store/base.py`, both directly relevant to anyone
  implementing a third-party `StorageBackend` — the exact audience `ontolith.store`'s own
  `StorageBackend` export already serves) had no import path shorter than `ontolith.store.base`. Now
  exported from `ontolith.store` alongside `StorageBackend`.

**One near-miss, caught before merge.** An earlier pass of this same audit found `schema/linkml.py`'s
`to_yaml`/`from_yaml` had the identical "module `__all__` exists, package never re-exports it" shape
and added them to `ontolith.schema`'s exports — verified broken immediately after
(`ModuleNotFoundError: No module named 'yaml'` on plain `import ontolith.schema`): `pyyaml` is gated
behind the optional `interop` extra, not a base dependency, and `schema/linkml.py` imports it at
module level. `schema/rdf.py`'s own docstring already documented this exact class of trap for its
own `rdflib` dependency ("mirrors `schema/linkml.py`'s own `pyyaml` opt-in, which for the identical
reason isn't re-exported") — but `linkml.py`'s own docstring never stated it, which is what let this
slip through on a first pass. Reverted before merge; `linkml.py`'s docstring now states the same
reasoning `rdf.py`'s always has, and `test_public_api_surface.py` gained a new regression test
(`test_pinned_packages_import_cleanly_without_any_optional_extra`, a subprocess-isolated check that
poisons `sys.modules` for every optional-extra package — `yaml`, `rdflib`, `duckdb`, `fastapi`,
`strawberry`, `mcp` — before importing the full pinned surface) so this exact mistake fails CI
automatically next time, mutation-tested against the reverted change itself to confirm it actually
catches it.

**One scoping question considered, and reconsidered by round 1 below: `ontolith.plugins.reference`
stays out of this policy's pinned surface.** Three of the four reference plugins (`CsvImporter`/
`JsonExporter`/`RequiredFieldsValidator`, KI-010) predate this ADR; the fourth (`RdfExporter`,
ADR-0036) postdates it by about six weeks (see round 1's own correction below — an earlier draft of
this paragraph claimed all four predated it "by four days," true only for the first three).
Deliberately left out regardless of date, not an oversight found late: they're discovered and
invoked via `pyproject.toml` entry points (`PluginRegistry`'s own loading path), not typically
imported by class name from application code the way `Ontology`/`QueryBuilder` are — "proves plugin
discovery... with working code" (the package's own docstring) describes reference/example
implementations a real deployment is expected to replace or extend, not a stable library surface
this policy's SemVer guarantee is about. `RdfExporter` itself already isn't even re-exported from
`plugins.reference`'s own `__init__.py`, for the identical optional-`rdflib`-extra reason this
Update's near-miss paragraph names above — internally consistent with staying out of the pinned
surface entirely. `PluginRegistry.register()` takes a string entry-point name, not a class — there
is no by-class-name loading path a subclass of a reference plugin would need pinned, and with
`isolate=True` (the default) `LoadedPlugin.instance` is a proxy for which `isinstance` against the
real class doesn't hold anyway (ADR-0051) — the subclassing scenario that would argue for pinning
these doesn't actually arise.

**Also found and fixed**: `ontolith.core`'s package docstring still listed "Future: meta-model, IR,
validation" — stale since M1/M3; that work shipped as `ontolith.schema`, not `ontolith.core`.
Reworded to say so. This ADR's own Context (line 16, above) also named only ten packages, predating
`ontolith.store.migrations` (added when M4 Workstream 4/ADR-0052 shipped `format_version` migration
reporting).

**Round 1 review found the audit above was itself incomplete, on two counts, plus one factual error
in its own record — all fixed and independently re-verified by direct reproduction:**

- **HIGH — the audit's own stated method (a class/constant with its own module `__all__`,
  unreachable from any pinned package) also matches `ProposalState`, `ContradictionState`, and
  `safe_rationale_history`, all missed on the first pass.** `ProposalState`/`ContradictionState`
  (`govern/proposal.py`/`govern/contradiction.py`) are the `Literal` type aliases annotating
  `Proposal.state`/`Contradiction.state` — both documented, pinned attributes of already-pinned
  classes, and three of the four shipped interfaces (`rest.py`, `graphql.py`, `cli.py`) already
  derive their own accepted-values sets from `get_args(ProposalState)`, direct evidence the alias is
  meant as the single source of truth, not an implementation detail. `safe_rationale_history`
  (`govern/contradiction.py`) is the defensive coercion every reader of `Contradiction.metadata`'s
  open `rationale_history` blob needs (KI-071/KI-075/KI-076) — an SDK consumer holding a pinned
  `Contradiction` and reading its pinned `.metadata` attribute is in exactly that position. All three
  now exported from `ontolith.govern`.
- **HIGH — the `TokenAuthProvider`/`hash_token` circular-import claim in the audit's own
  `AuthProvider` paragraph (and echoed in both `identity/__init__.py`'s and `token_auth.py`'s own
  docstrings) was false as stated, and the audit's own text claimed it had been independently
  verified.** Reproduced directly: adding `from ontolith.identity.token_auth import TokenAuthProvider,
  hash_token` to `identity/__init__.py` **after** the existing `principal` import works cleanly, in
  every entry order tried (`ontolith`, `ontolith.identity`, `ontolith.store`, `ontolith.govern`,
  `ontolith.interfaces.rest`, `.token_auth` directly). The cycle is real but order-dependent, not
  absolute: `token_auth.py` imports `StorageBackend` from `store.base`, which transitively imports
  `govern.contradiction`, which triggers `govern.policy`'s own `from ontolith.identity import
  Principal, ...` reaching back into the still-initializing `ontolith.identity` module — that reverse
  import only fails if `Principal` hasn't been bound in this module's namespace yet, i.e. only if
  `token_auth` is imported *before* `principal` (reproduced that failure too, deliberately, to
  confirm the mechanism). The required order is exactly what `ruff`'s own isort rule (`I001`, already
  CI-blocking) enforces — `token_auth` sorts alphabetically last among this package's five
  submodules — so it isn't a silent footgun in practice, but the ADR's claim of an absolute block was
  wrong regardless. `TokenAuthProvider`/`hash_token` are now exported from `ontolith.identity`,
  positioned after `principal`; `identity/__init__.py`'s own docstring now states the accurate,
  order-dependent mechanism instead of the false absolute one.
- **MEDIUM — `ontolith.interfaces.rest`/`ontolith.interfaces.graphql` were left unaddressed by
  Decision #1's original exclusion list**, which named only `interfaces.cli`/`interfaces.mcp` as
  excluded and said nothing about the other two `interfaces` submodules, despite both having their
  own `__all__` (`create_rest_app`, `create_graphql_app`) and this same Update's own `AuthProvider`
  paragraph justifying that promotion by pointing at their signatures. Resolved (see the revised
  Decision #1 above): both ARE part of this policy — `create_rest_app`/`create_graphql_app` are
  genuine Python call-signature surfaces, unlike CLI flags or MCP tool schemas — pinned the same way
  `ontolith.store.duckdb` already is (part of the tested surface, exempt from the "importable with no
  optional extras" guarantee, since both need their own extra to import at all).
- **LOW — the `plugins.reference` paragraph's "predate this ADR by four days" claim was true for
  three of the four plugins and false for the fourth.** `RdfExporter` was added 2026-08-27 (`ADR-0036`
  update), roughly six weeks *after* this ADR's own 2026-07-15 date, not four days before it — only
  `CsvImporter`/`JsonExporter`/`RequiredFieldsValidator` (2026-07-11) predate it. Corrected above; the
  underlying scoping decision (all four stay out) is unaffected — see the revised paragraph above for
  why it doesn't depend on the date either way.
- Also derived `test_pinned_packages_import_cleanly_without_any_optional_extra`'s (new in this same
  Update) import list from `_MODULES` itself rather than a separately hardcoded list, closing the
  exact "a new pinned package could silently miss this check" gap the test itself exists to prevent
  for the surface at large.

Every claim in this round-1 paragraph was independently re-verified against source and by direct
execution before being written, not carried forward from the review's own report.

**Declaration: the resulting, now-audited surface is the 1.0 API-freeze candidate.** This closes
Implementation Plan §14's open question 6 — "exactly which symbols are covered by the SemVer
guarantee at 1.0" — definitively: every symbol in `tests/unit/test_public_api_surface.py`'s
`_EXPECTED` dict, as of this Update (including round 1's own additions), across all thirteen pinned
packages (the original eleven, plus `ontolith.interfaces.rest`/`ontolith.interfaces.graphql`,
resolved by round 1 above). "Freeze" at this stage (the
project is at version `0.0.1`, still pre-1.0, with the security-review and complete-docs M4
workstreams still ahead) means: this is the committed baseline going forward — any further addition,
removal, or rename before the actual 1.0 tag still follows this ADR's existing Decision #2 (pre-1.0
minor versions may break the surface, but every break **MUST** get a CHANGELOG `**Breaking:**`
marker), the same discipline already in force; it does not mean the surface is now literally
immutable pre-1.0. What changes at the *actual* 1.0 tag (a separate, later release event this
workstream does not itself trigger) is enforcement: `ci.yml`'s `griffe check` step drops
`continue-on-error: true` and starts blocking, matching KI-020's own resolution note and this ADR's
original Decision #4. The signature-level gap Consequences already named (`griffe` catches
export-set changes and, when made blocking, a subset of signature changes it can diff; the pinned
`__all__` test alone never did) remains a real, accepted residual either way — not resolved by this
Update, not newly introduced by it either.

## References

- Implementation Plan §2 (M3 exit criteria), §5 (quality gates table), §7.2 (SemVer commitment), §14.6 (open question, now resolved — see Update above)
- `CHANGELOG.md` (existing informal `**Breaking:**` convention, now formalized)
- ADR-0016 (DuckDB Second Backend — the sibling M3 exit criterion this ADR's introduction contrasts against)
- ADR-0052 (on-disk storage-format migrations — added `ontolith.store.migrations` to the pinned surface, the package this Update's own audit found this ADR's Context paragraph hadn't caught up to)
- `docs/known-issues.md` KI-020 (the deferred `griffe` CI gate — already resolved since M3 via ADR-0026, referenced by the Update above, not resolved by it)
- ADR-0026 (Security CI-Hardening Pass and Public API Diff Gate — actually wired up the `griffe check` step this Update's enforcement-timing paragraph relies on)
