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

## References

- Implementation Plan §2 (M3 exit criteria), §5 (quality gates table), §7.2 (SemVer commitment), §14.6 (open question)
- `CHANGELOG.md` (existing informal `**Breaking:**` convention, now formalized)
- ADR-0016 (DuckDB Second Backend — the sibling M3 exit criterion this ADR's introduction contrasts against)
- `docs/known-issues.md` (new entry tracking the deferred `griffe` CI gate)
