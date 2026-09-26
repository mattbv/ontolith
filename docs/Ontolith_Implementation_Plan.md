# Ontolith — Implementation Plan & Engineering Playbook

*Execution plan for building Ontolith. Turns the SPEC (v0.1) into a buildable project: sequencing, repository setup, architecture rules, quality gates, testing strategy, CI/CD, security, and success metrics. Tool choices are opinionated defaults — swap with reason, but record the reason as an ADR.*

| | |
|---|---|
| **Document type** | Implementation plan / engineering playbook |
| **Status** | Draft for review |
| **Builds on** | PRD v0.2, SPEC v0.1 |
| **Runtime** | Python ≥ 3.11 |
| **License** | Apache-2.0 (open-core) |
| **Last updated** | 2026-06-20 |

---

## 1. Principles for execution

1. **Risk-first sequencing.** Build the two riskiest things early: bitemporal/conflict **correctness** (proved with property tests) and traversal **performance** (baselined from MVP). Everything else is comparatively low-risk.
2. **Conformance is the spec made executable.** The SPEC §19 vectors become a reusable test kit that is a merge-blocking gate and lets third-party storage backends self-certify.
3. **The dependency rule is sacred.** Domain logic never imports a concrete adapter. Enforced in CI, not by convention.
4. **Determinism by construction.** Time and IDs are injected ports, so bitemporal behavior is testable and replayable.
5. **Boring foundations.** Stable, ubiquitous tools; the novelty budget goes into the product, not the toolchain.

---

## 2. Phasing & sequencing

Phases map to the PRD roadmap but are expressed as engineering milestones with **exit criteria** (a milestone isn't "done" until its gate passes). Build order respects the dependency chain: **core → store → identity → govern → query → interfaces → plugins**.

| Milestone | Maps to | Scope | Exit criteria (gate) | Status |
|---|---|---|---|---|
| **M0 — Foundations** | pre-0.1 | Repo, tooling, CI skeleton, ports/protocols stubbed, Clock & ID providers, ADRs 1–8 recorded, error taxonomy | Green CI on an empty-but-wired skeleton; a contributor can clone → install → test → lint in one command; `import-linter` contract active | ✓ Complete |
| **M1 — Substrate (0.1)** | MVP | Meta-model + IR, class DSL, SQLite backend, entities/assertions (append-only), identity basics + capabilities, proposal→accept + threshold policy, basic query builder, Python SDK, CLI | Core conformance vectors pass; `examples/quickstart.py` runs end-to-end; **traversal benchmark baseline captured**; coverage gate met | ✓ Complete |
| **M2 — Collaboration (0.2)** | collaboration | Review workflow, bitemporal time-travel, conflict (supersession + contradictions), MCP server (read/propose/flag/provenance), trust levels, delegation, LinkML-aligned YAML, first 3 reference plugins | **Full §19 conformance** incl. conflict/bitemporal/delegation vectors; property tests green; MCP exposes **no write tool** (test-enforced); LinkML round-trip golden tests pass | ✓ Complete |
| **M3 — Extensible (0.3)** | open boundaries | Plugin registry + stable extension API, full hybrid retrieval, REST + GraphQL, one scale-out backend adapter, LinkML bridge then RDF/OWL | **Backend conformance kit passes on a 2nd backend**; plugin contract tests green; public API stability policy begins | Exit criteria met (2026-07-16); scope complete — REST done (ADR-0021/0022), hybrid retrieval done (KI-018), DuckDB is the 2nd conformance backend, RDF/OWL bridge done (ADR-0036, export only), **GraphQL done (ADR-0037, query/propose/review only)** |
| **M4 — Production (1.0)** | govern at scale | Hardened policy engine, plugin sandboxing, perf budgets met, complete docs, migration tooling, SPEC §18 observability (ADR-0044) | Security review passed; **performance budgets met** (§9); SemVer 1.0 API freeze; on-disk `format_version` frozen | Started 2026-09-17. Workstream 1 (perf budgets) done — all five §9 rows have a valid benchmark and pass with large margins (see §9 below); still informational, not a CI-blocking gate. Found and filed **KI-100** (open-contradiction-extension cost) along the way. Workstream 2 (SPEC §18 observability, ADR-0044) tier (a) done 2026-09-19 — `ObservabilitySink` port + `StdlibLoggingSink`/`NullObservabilitySink`/`RecordingObservabilitySink`, wired into `Ontology`, all four ad hoc `logging.getLogger()` call sites (five emission sites) migrated; tiers (b)/(c) (events/metrics) not started. Workstream 3 (plugin process isolation, ADR-0051) done 2026-09-20 — `PluginRegistry.register(isolate=True)` default, plugin entrypoints run in a spawned child process, network/filesystem enforced via seccomp on Linux (two CRITICAL bypasses of the process boundary itself, found in review round 1, fixed and re-verified before merge, see ADR-0051's Update); macOS/Windows OS-level enforcement and `.query()`/`.as_of()` (KI-101) remain open. Workstream 4 (migration tooling, SPEC §15, ADR-0052) done 2026-09-21 — `format_version` tracked per backend, `SQLiteBackend`/`DuckDBBackend` refuse (`SchemaError`) an existing file below the current format rather than silently upgrading it, `ontolith db migrate [--dry-run]`/`db status` apply or preview pending migrations explicitly; the two historical ad hoc DDL fixups (KI-060, KI-078) are now registered, reversible migrations. Five review rounds — rounds 2-4 each found a real bug in the same narrow area (a migration's `up()` needing to tolerate one more state of its own target than anticipated), round 5 clean; filed **KI-104** (declarative migration registry, follow-up for a third migration) |
| **vNext** | live commons | CRDT multi-writer, assisted conflict resolution, federation, UI, marketplace | per-feature specs | Not started |

**Critical path:** M0 → M1 (core+store) → M2 (conflict+bitemporal+MCP). M2 is where the product becomes itself; protect its timeline. REST/GraphQL and scale-out (M3) can parallelize once the ports are stable.

**Status as of 2026-09-21** (updated opportunistically, not on every change — treat as a snapshot, verify against `CHANGELOG.md`/`git log` for anything time-sensitive): M0–M2 fully complete. M3's three named exit criteria are all met, and its full scope column is now complete too: the RDF/OWL bridge (ADR-0036 — schema as OWL ontology plus active-assertion instance data, export only, `rdflib`-backed; no import direction yet) and the GraphQL interface (ADR-0037 — `strawberry`-backed, originally scoped to query/propose/review operations per SPEC §14.3's literal wording, deliberately narrower than REST's own extended write/admin surface; widened once since, to add entity creation — KI-082, ADR-0037's own 2026-09-08 update) both shipped. `docs/known-issues.md`'s backlog was, at that point, **empty** (KI-099/KI-080/KI-085/KI-087/KI-083/KI-089/KI-082/KI-090/KI-091/KI-088/KI-084/KI-092/KI-086/KI-081/KI-093/KI-094/KI-096/KI-095/KI-097/KI-098 — all twenty pre-M4 audit KIs and their previously-filed follow-ups — are resolved; KI-088/KI-084/KI-086/KI-081/KI-080 were each one of the original nine) except two carried-forward partials (KI-014, plugin network/filesystem sandboxing, open since M3; KI-031, schema `required` enforcement, deferred since M3 per ADR-0028 — `value_type` enforcement itself is resolved, only `required` remains, tracked nowhere else since its own forward-pointers KI-041/KI-042 are both resolved) — neither blocking M3; see below for KI-100, filed the same day once M4 work itself began. KI-080 (extend `cardinality="many"` to `time_varying` supersession, revisiting ADR-0017's recorded scope) needed a genuine design decision — an explicit `supersedes` hint on the write paths was chosen over always-coexisting or leaving the gap open, recorded in **ADR-0050** — the last original audit item, deliberately left for last. It shipped SDK-only by design, with interface (REST/GraphQL/MCP/CLI) parity filed and then closed separately as **KI-099**, mechanical parity work with no design question of its own. KI-001 through KI-079 are otherwise resolved, for KI-011 a deliberate non-implementation rather than a gap, or for KI-031 the still-open `required` partial named above. **M4 started 2026-09-17**, scoped workstream by workstream rather than as one big-bang plan: performance budgets (§9) first, since it's diagnostic (does the SQLite-default architecture actually hold up before investing in the harder design work) and had partial groundwork already. Found the existing benchmark suite's coverage claim didn't match its own numbers: `propose`+policy-eval+commit's budget row had no *valid* benchmark — its stand-in (`assert_literal`, repeated on one `(subject, predicate)`) accidentally measured unbounded contradiction-extension cost instead of a stable write cost (reproduced directly — the open contradiction's `member_ids` genuinely grew one entry per benchmark round). Fixed, plus a new benchmark for the budget's own actually-named `propose()` path. All five §9 rows now have a valid, stable benchmark (see §9 below for the per-row breakdown and dataset caveat — the hybrid-query row's own dataset is two orders of magnitude smaller than the other four's, not the same 100k-assertion dataset throughout) and pass comfortably — informational only, not yet a CI-blocking gate (that's a separate, later decision within this same workstream). Found while fixing this: the open-contradiction-extension code path has a real, measured, unbounded-with-size per-write cost and no benchmark of its own at all — filed as **KI-100**, reopening `docs/known-issues.md`'s backlog to 1 fresh item (3 non-resolved entries total at that point, counting the two carried-forward partials named above — KI-014 and KI-031 — neither of which KI-100 changes; two more, **KI-101** and **KI-102**, were filed the following day building Workstream 3, see below — 5 non-resolved entries as of this update). **Workstream 2 (SPEC §18 observability, ADR-0044) tier (a) done 2026-09-19**: new `core/observability.py` ships the `ObservabilitySink` ABC (`log`/`record_event`/`record_metric`, exactly the shape ADR-0044 already decided) plus `StdlibLoggingSink` (the production-safe default — writes through stdlib `logging`, not a no-op, so none of the migrated call sites regresses to silence for a deployment that hasn't wired up an `observe/`-resident adapter sink), `NullObservabilitySink` (explicit silence), and `RecordingObservabilitySink` (test double). `Ontology` gained an `observability` parameter defaulting the same way `clock`/`id_provider` already do; `pyproject.toml`'s import-linter contract gained the `ontolith.observe` entry ADR-0044 named as still-needed. All four of ADR-0044's own named ad hoc `logging.getLogger()` call sites (five emission sites — REST's `_handle_ontolith_error`, GraphQL's `_OntolithSchema.process_errors` ×2, MCP's `_error_response`, plugin registry's unenforced-capability warning) now log through `kb.observability` instead — see ADR-0044's own Update section for why the default isn't a no-op despite the ADR's original "always safe to make" wording, and CHANGELOG's M4 section for the full list. Tiers (b) (four named lifecycle events) and (c) (seven-metric surface, its own follow-up ADR) remain not started. **Workstream 3 (plugin process isolation, ADR-0051) done 2026-09-20**: `PluginRegistry.register()` defaults to `isolate=True` — a plugin's one protocol entrypoint now runs in a freshly spawned child process (`plugins/sandbox/`), with its `kb` view and any other live argument (e.g. an `io.StringIO` export target) proxied back over one IPC pipe. On Linux with a working `pyseccomp`/libseccomp install, `capabilities.network`/`.filesystem` are now genuinely enforced at the OS syscall level (seccomp, `ERRNO(EPERM)`); macOS/Windows get no OS-level enforcement in this pass, honestly documented as a residual gap rather than approximated with an unverifiable mechanism — KI-014 stays "partially resolved," now scoped precisely to non-Linux platforms. `.query()`/`.as_of()` are unsupported inside an isolated call (no shipped reference plugin needs either); filed as **KI-101**. There is also no timeout on an isolated call — a hung plugin blocks the caller indefinitely; filed as **KI-102**. Five review rounds (architecture + a dedicated security review, then three further rounds) found the process boundary itself was bypassable as first built — a plugin's own message to the parent was unpickled with plain `pickle.loads()` (arbitrary code execution, reproduced) and the dispatch loop invoked any method name the child asked for on the real, unproxied view with no allow-list (reproduced privilege escalation via `__setattr__`, no pickle needed) — both fixed and independently re-verified by reproducing both attacks against the fixed code before merge, and both held under every later round's re-reproduction too; rounds 2, 3, and 4 also each found a real regression in the prior round's own fix (most notably: a plugin's own documented `ValueError`/`TypeError` was briefly mislabelled as a protocol violation instead of propagating normally, round 3; round 4 then closed an unguarded send-side `BrokenPipeError` path round 3's own refactor had left open), all fixed and re-verified; a fifth round found no further code-level issues. See ADR-0051's own Update section for the full record. The "no live object graph reaches the plugin" claim now holds because of that fix, not by the original design. **Workstream 4 (migration tooling, SPEC §15, ADR-0052) done 2026-09-21**: SPEC §15 makes three promises — an on-disk `format_version`, a declared rewrite strategy plus dry-run mode for breaking changes, and reversible-or-explicitly-irreversible migrations — none of which existed before this workstream; the backend's own two historical DDL changes (KI-060's `principal_credential.issued_by`/`.revoked_by`, KI-078's `proposal.reviewers`) were handled by an ad hoc `PRAGMA table_info`/`information_schema.columns` check plus an idempotent `ALTER TABLE`, applied silently on every connect. New `format_version` single-row table, tracked independently per backend (`store/sqlite/migrations.py`, `store/duckdb/migrations.py`, both at `CURRENT_FORMAT_VERSION = 3` today); `SQLiteBackend`/`DuckDBBackend` now refuse (`SchemaError`) to open an existing file below the current format rather than silently upgrading it — a fresh, empty file is unaffected. `migrate_file(path, *, dry_run=False)` is the explicit, standalone action that applies (or, dry-run, only previews, writing nothing) pending migrations — it does not go through the normal backend constructor at all, since that constructor would refuse exactly the file this function needs to open; exposed as `ontolith db status`/`ontolith db migrate [--dry-run]` (SQLite only, matching `Ontology.connect()`'s own scope). Both historical migrations are formalized as registered, declared-`reversible=True` entries; DuckDB's `principal_credential` reversal needs to drop and recreate its secondary index around the `DROP COLUMN` calls, since DuckDB (pinned `1.5.4`, verified directly) refuses `ALTER TABLE ... DROP COLUMN` on any table with a secondary index at all, even one on an unrelated column. Data migration/backfill for a renamed or retyped *domain* predicate against already-stored assertions (ADR-0034's own "scope (b)") remains a distinct, still-open problem this workstream does not solve — see ADR-0052's own Consequences. Five review rounds: rounds 2 through 4 each found a real bug in the same narrow area — a registered migration's `up()` needing to tolerate one more state of its own target table/column than the previous round anticipated (already applied; the target table absent entirely; the target shadowed by a view) — round 5 found none, backed by an exhaustive empirical sweep of every reachable table/column-state combination (100 SQLite files, each migrated then re-opened). Filed **KI-104**: the per-`up()` defensiveness this arc converged on is correct and exhaustively verified today but not centralized — a declarative, centrally-applied registry is recommended as a follow-up once a third migration is actually added, not before. See ADR-0052's own Update section for the full record. **Workstream 5 (SemVer 1.0 API-surface freeze, §14 open question #6) done 2026-09-21**: ADR-0019 (M3) already defined the public surface (union of every package's `__all__` plus documented public methods on the classes they export); the `griffe check` informational CI diff gate it deferred was separately wired up in M3 via ADR-0026 (KI-020, resolved then, not by this workstream) — blocking flips on at the actual 1.0 tag, a separate later event this workstream doesn't itself trigger. This workstream audited the ADR-0019 surface end to end — every module with its own `__all__` that the *package*-level `__init__.py` might not have picked up, the exact shape a later addition can silently miss — and found (across two review rounds) seven real gaps across four packages: `AsOfView` (the return type of `Ontology.as_of()`, structurally identical to the already-exported `QueryBuilder`, but never itself importable), `AuthProvider`/`TokenAuthProvider`/`hash_token` (the abstract port every `create_*_app` factory takes, plus its one concrete implementation), `VECTOR_SCOPES`/`DEFAULT_NAMESPACE` (both directly relevant to a third-party `StorageBackend` implementer), and `ProposalState`/`ContradictionState`/`safe_rationale_history` (round 1's own find — type aliases for pinned attributes of pinned classes, plus a defensive helper every reader of `Contradiction.metadata` needs) — now exported from `ontolith`/`ontolith.identity`/`ontolith.store`/`ontolith.govern` respectively. One near-miss caught before merge: an earlier pass of the same audit briefly re-exported `schema.linkml`'s `from_yaml`/`to_yaml` too, verified broken immediately (`pyyaml` is an optional `interop`-extra dependency, not a base one, and `linkml.py` imports it at module level) and reverted — a new subprocess-isolated regression test (`test_pinned_packages_import_cleanly_without_any_optional_extra`, its own import list derived from the pinned-module set itself after round 1 found the first version hardcoded a separate, driftable list) now catches this exact mistake automatically, mutation-tested against the reverted change itself. Round 1 also found the `TokenAuthProvider` exclusion's original "circular import" justification was false (it's order-dependent, not absolute, and already protected by `ruff`'s own CI-blocking isort rule) and that `ontolith.interfaces.rest`/`ontolith.interfaces.graphql` had been left unaddressed by ADR-0019's own exclusion list despite this same workstream's `AuthProvider` reasoning applying to them too — both resolved, the latter by pinning them the same way `ontolith.store.duckdb` already is (part of the tested surface, exempt from the "no optional extra needed" guarantee). `ontolith.plugins.reference`'s four reference plugins were considered and deliberately left out of the pinned surface regardless (discovered via `pyproject.toml` entry points, not typically imported by class name — illustrative/example code, not the stable library surface this policy is about). Declares the resulting, now-audited thirteen-package surface the 1.0 freeze candidate — see ADR-0019's own Update section for the full record, including why "freeze" at version `0.0.1` means "committed baseline, still governed by the existing pre-1.0 CHANGELOG-`**Breaking:**` discipline," not literal immutability before the real 1.0 tag. Remaining M4 workstreams, in the order settled with the user: `format_version` freeze (Workstream 4 gave `format_version` something to freeze; the freeze decision itself is separate), security review, complete docs.

---

## 3. Repository setup

### 3.1 Strategy: thin-core monorepo + satellite plugin packages

- **One monorepo** for the engine: a single installable `ontolith` package (src-layout) with the SPEC's internal layers as subpackages. The **core has minimal runtime dependencies**; interfaces and heavy integrations are **optional extras** or **separate packages**.
- **Satellite repos/packages** for plugins with heavy or niche deps (`ontolith-linkml`, `ontolith-rdf`, connectors like `ontolith-slack`). This keeps the core dependency-light and lets the ecosystem move independently.

### 3.2 Tooling (defaults)

| Concern | Choice | Notes |
|---|---|---|
| Env & deps | **uv** | fast, lockfile-based, reproducible |
| Build backend | **hatchling** | standard, src-layout friendly |
| Lint + format | **ruff** | replaces black/isort/flake8 |
| Types | **mypy --strict** (gate) | optionally also run `pyright`; `ty` is emerging — evaluate, don't depend |
| Tests | **pytest** + **pytest-cov** + **hypothesis** + **pytest-benchmark** | property tests are first-class here |
| Arch rule | **import-linter** | enforces the dependency rule |
| Security | **pip-audit**, **bandit**, **gitleaks**, **cyclonedx** (SBOM) | |
| Docs | **mkdocs-material** + **mkdocstrings** | API ref from docstrings |
| Hooks | **pre-commit** | runs the fast gates locally |
| Release | **trunk-based** + conventional commits + **release automation** | PyPI **Trusted Publishing** (OIDC, no tokens) |

### 3.3 Layout

```
ontolith/
├── pyproject.toml            # uv + hatchling; extras: [vec],[rest],[graphql],[mcp],[all],[dev]
├── uv.lock
├── LICENSE                   # Apache-2.0
├── README.md  CONTRIBUTING.md  CODE_OF_CONDUCT.md  SECURITY.md  CHANGELOG.md
├── .pre-commit-config.yaml
├── .github/
│   ├── workflows/{ci.yml, release.yml, nightly.yml, security.yml}
│   ├── ISSUE_TEMPLATE/  PULL_REQUEST_TEMPLATE.md
├── docs/
│   ├── adr/        # ADR-0001..0008 = the PRD §16 decisions; +0009 monorepo, 0010 toolchain, 0011 hexagonal
│   ├── rfcs/       # RFC process for significant changes
│   └── ...         # mkdocs site, quickstart, guides
├── src/ontolith/
│   ├── core/        # meta-model, IR, validation, Clock & IdProvider ports
│   ├── schema/      # class DSL, YAML loader, codegen, migration
│   ├── store/
│   │   ├── base.py  # StorageBackend Protocol (port)
│   │   └── sqlite/  # default adapter (+ sqlite-vec, pinned)
│   ├── identity/    # principals, auth ports, capabilities, delegation
│   ├── govern/      # proposals, policy (PURE), review, conflict
│   ├── query/       # builder, traversal, hybrid retrieval
│   ├── plugins/     # registry, protocols, lifecycle, sandbox
│   ├── interfaces/  # sdk, cli, rest, graphql, mcp
│   └── observe/     # structured logging, metrics, audit
├── conformance/     # reusable SPEC §19 kit (backends import & run this)
├── tests/{unit, property, integration, contract, benchmarks}/
└── examples/{quickstart.py, org_brain/, agent_fleet/, living_review/}
```

### 3.4 Dependency hygiene

- **Core runtime deps minimal:** pydantic, typer (CLI), python-ulid, structlog. SQLite is stdlib.
- **`sqlite-vec` lives in the `[vec]` extra and is version-pinned** — it is pre-v1, so a minor bump can break (SPEC §13 note). Isolate it behind the storage port so it can be swapped.
- Renovate/Dependabot for updates; lockfile committed; `pip-audit` in CI.

### 3.5 Governance files

- **Contribution model:** a **CLA** is recommended for open-core (preserves the right to offer proprietary builds per PRD §12); **DCO sign-off** is the lighter alternative if contributor friction matters more than relicensing flexibility.
- `SECURITY.md` with a coordinated-disclosure address; branch protection requiring green CI + 1 review; trademark notice on the name.

---

## 4. Architecture best practices

### 4.1 Ports & adapters (hexagonal)

The domain (`core`, `schema`, `govern`, `query`) depends **only on abstract ports**: `StorageBackend`, `Embedder`, `AuthProvider`, `PolicyStrategy`, `Clock`, `IdProvider`. Concrete adapters (`store/sqlite`, embedders, OIDC) implement them. This is what makes the SQLite-default-but-pluggable promise real.

**Dependency rule (CI-enforced via import-linter):**
```
core, schema, govern, query   ──MUST NOT import──▶  store/sqlite, interfaces/*, any concrete adapter
interfaces                    ──may import──▶       core, govern, query (via the SDK facade only)
plugins                       ──implement──▶        ports; never import domain internals
```

### 4.2 Invariants enforced in code & tests

- **Append-only assertions.** Only `status`, `valid_to`, and successor links mutate. A property test asserts no code path edits `value`.
- **Policy purity.** `govern/policy` performs no I/O and is deterministic; a test harness denies it storage/network access.
- **Determinism.** No `datetime.now()` or `uuid4()` inside domain logic — both come from injected `Clock`/`IdProvider`, so bitemporal tests are reproducible.
- **Confidence is never auto-combined** (v1); the `metadata` blob round-trips losslessly.
- **Accountable owner.** Creating an `ai` principal without a resolvable owner is rejected at the boundary *and* by a DB `CHECK`.
- **No-write-over-MCP.** A test asserts the MCP tool registry contains no direct-write tool.

### 4.3 Boundary discipline

- **Validate at edges, trust within.** Pydantic validation at SDK/REST/MCP boundaries; inner domain functions take already-validated value objects (no re-validation in hot loops).
- **Typed public API, no `Any`.** Public surfaces are fully typed; `py.typed` shipped.
- **Stable error taxonomy** (SPEC §16) with machine-readable `code`s mapped consistently to HTTP/MCP.
- **One transaction per proposal acceptance** (SPEC §12); conflict handling runs inside it.

### 4.4 Decision records

Every significant choice is an **ADR** (MADR format). Seed the log with the eight PRD §16 decisions as ADR-0001…0008 so the rationale travels with the code. New cross-cutting changes go through a lightweight **RFC** in `docs/rfcs/` before implementation.

---

## 5. Code quality gates

The merge-blocking set (runs in CI on every PR; the fast subset runs in pre-commit):

| Gate | Tool | Threshold | Blocking |
|---|---|---|---|
| Format | ruff format --check | clean | ✓ |
| Lint | ruff check | zero errors | ✓ |
| Types | mypy --strict (src) | zero errors | ✓ |
| Unit/integration tests | pytest | all pass | ✓ |
| Coverage | pytest-cov | **≥ 90% domain** (`core`,`govern`,`query`), ≥ 85% overall; **100% on `govern/conflict` & bitemporal** | ✓ |
| **Conformance** | SPEC §19 kit | all vectors pass | ✓ |
| Dependency rule | import-linter | contract holds | ✓ |
| Property invariants | hypothesis | no falsifying example | ✓ |
| Vulnerabilities | pip-audit | no unresolved high/critical | ✓ |
| Static security | bandit | no unsuppressed mediums+ | ✓ |
| Secrets | gitleaks | none | ✓ |
| Public API docstrings | interrogate | ≥ 95% on public API | ✓ |
| Commit messages | conventional commits | conformant | ✓ (enables changelog) |
| Perf regression | pytest-benchmark | no > 15% regression vs baseline | informational → blocking by M4 |
| Public API surface | griffe diff | no unintended breaking change | warn pre-1.0, block post-1.0 |

**Definition of Done** for a unit of work: gates green; ADR/RFC updated if a decision changed; docstrings + a docs/example touched if public behavior changed; conformance/property tests added for new invariants.

---

## 6. Testing strategy

Layered, with property-based and conformance testing doing the heavy lifting because the hard parts (bitemporal, conflict) are easy to get subtly wrong.

- **Unit** — pure functions and value objects; fast, no I/O.
- **Property-based (Hypothesis)** — the correctness backbone:
  - *Bitemporal:* any sequence of assert/supersede/retract → `as_of(t)` and `valid_at(t)` reconstruct a consistent state; history is monotonic.
  - *Conflict routing:* `static` + differing value ⇒ contradiction (never overwrite); `time_varying` ⇒ supersession with closed prior window; corroboration never auto-combines confidence.
  - *Append-only:* no operation mutates an assertion's `value`.
- **Conformance kit** — the SPEC §19 vectors packaged so **any `StorageBackend` can self-certify**; runs against the SQLite default and every other backend.
- **Contract tests** — abstract test bases per plugin protocol (Importer/Exporter/Reasoner/Validator/Embedder/PolicyStrategy/StorageBackend).
- **Golden tests** — schema codegen round-trips (`classes → IR → YAML → IR → classes` is idempotent); LinkML export fidelity.
- **Integration** — SDK ↔ SQLite ↔ vec; REST/GraphQL contract; MCP tool-schema validation + behavior (propose returns a proposal, model captured, no write tool).
- **Benchmarks (pytest-benchmark)** — the risk watch:
  - traversal latency vs depth on synthetic graphs (the #1 perf risk),
  - proposal throughput, query p50/p95, vector recall@k.
  - Baseline in M1; tracked nightly; budgeted by M4.

---

## 7. CI/CD

### 7.1 Pipeline

```
ci.yml (every PR):
  matrix: python [3.11, 3.12, 3.13] × os [ubuntu, macos, windows]
  steps: uv sync → ruff format/check → mypy --strict → import-linter
       → pytest (unit, property, integration) + coverage gate
       → conformance kit → docstring + commit-lint
security.yml (PR + weekly): pip-audit · bandit · gitleaks · SBOM (cyclonedx)
nightly.yml: full benchmarks → publish trend; long-running hypothesis profiles
release.yml (tag): build → publish to PyPI via Trusted Publishing → mkdocs deploy → GitHub release notes
```

### 7.2 Branching & releases

- **Trunk-based**: short-lived branches, PRs into `main`, branch protection (green CI + review).
- **SemVer** for the SDK; **pre-1.0**: minor versions may break, documented in CHANGELOG; **post-1.0**: breaking changes only on majors.
- **On-disk `format_version`** is independent of SDK version and frozen at 1.0; migrations are reversible or explicitly marked irreversible (SPEC §15). Mechanism shipped 2026-09-21 (ADR-0052) — `format_version` tracked per backend, `ontolith db migrate`/`db status`; the freeze itself (declaring the current number final for 1.0) is a separate, still-open M4 exit criterion.
- Conventional commits drive an automated CHANGELOG and version bump.

---

## 8. Security & supply chain

- **Authz everywhere** (SPEC §17): capability checks at every interface boundary; accountable-owner invariant enforced in code and DB.
- **Agent safety:** MCP read/propose/flag only; no write tool; `propose` stamps author + model + `acting_as`.
- **Plugin sandboxing:** capability manifests, deny-by-default for storage/network/filesystem; **process isolation in v1, wasm/subprocess hardening + signed registry plugins by 1.0**. Process isolation shipped 2026-09-20 (ADR-0051) — real on Linux (seccomp); wasm/subprocess hardening for macOS/Windows and signed registry plugins remain not started (see §2's M4 row and KI-014).
- **Supply chain:** pinned lockfile, `pip-audit` gate, SBOM per release, Trusted Publishing (no long-lived tokens), pinned & isolated `sqlite-vec`.
- **Disclosure:** `SECURITY.md` with a private channel and an SLA; security fixes get backported to the latest minor.

---

## 9. Performance budgets

Set in M1 as baselines, enforced as gates by M4 (illustrative starting targets — tune against real workloads):

| Operation | Budget (laptop default backend) |
|---|---|
| `propose` + policy eval + commit | p95 < 50 ms |
| Single-entity `get` with provenance | p95 < 10 ms |
| 3-hop traversal, 100k-assertion KB | p95 < 200 ms |
| Hybrid query (symbolic prefilter + vector rerank), k=10 [^1] | p95 < 150 ms |
| `as_of` reconstruction, 100k assertions | p95 < 300 ms |

The 3-hop traversal budget is the canary for the SQLite-default decision; if real graphs blow it, that's the signal to graduate the scale-out backend (PRD §15).

**Status (2026-09-17, M4 started):** all five rows now have a valid, stable benchmark in
`tests/benchmarks/` (rows 1, 2, 3, and 5 in `test_traversal.py`'s 100k-assertion dataset; row 4,
hybrid query, in `test_hybrid_query.py`'s own separate, smaller 1k-entity/1k-assertion dataset —
see footnote [^1] below) and pass comfortably on their respective datasets — the hybrid-query row
is the tightest margin measured so far (double digits to low hundreds x under budget, machine- and
dataset-dependent), 3-hop traversal comfortably the loosest (three to four orders of magnitude
under budget); exact multiples aren't quoted here since they vary meaningfully run to run on a
laptop. Still informational only (`--benchmark-skip` in the default `pytest` run, no CI-blocking
assertion) — turning these into an actual M4 exit-gate check is separate, later work within this
same performance-budgets workstream, not done yet. `propose`+policy-eval+commit's own benchmark
(`test_bench_write_assert_literal`) previously existed but measured the wrong thing entirely (see
CHANGELOG's M4 section) — fixed, and a new `test_bench_propose_auto_accept` added for the budget
row's own actually-named operation; the two now measure comparably (roughly the same order of
magnitude), not "materially more expensive" as an earlier draft of this note claimed — `propose()`'s
extra proposal-construction/policy-evaluation overhead over `assert_literal`'s direct write is real
but modest, not dominant. Found while fixing this: the open-contradiction-extension code path
(`_apply_with_conflict_routing`'s "extend an already-open contradiction" branch) is entirely
unbenchmarked and has a real, measured per-write cost that grows with contradiction size — filed as
**KI-100**.

[^1]: As implemented (ADR-0020, M3), the actual order is the reverse: vector-search-first with an
overfetch, then intersected with any symbolic `.where()` filter, preserving vector rank order —
not a symbolic prefilter reranked by vector distance. See ADR-0020 §5 for the rationale (a
literal prefilter-then-rerank would re-embed and re-rank every symbolic candidate per call, which
does not stay within this budget under the pure-Python default `Embedder`) and its recorded
recall-cutoff consequence. The budget number itself (p95 < 150 ms) is unaffected and was
comfortably met in benchmarking (`tests/benchmarks/test_hybrid_query.py`) at 1k entities.

---

## 10. Documentation & DX

Docs are a feature, because **time-to-first-ontology** is a headline metric.

- **Quickstart** that goes from `uv add ontolith` to a working, queried ontology in **under 10 minutes** — itself a tested example.
- **Guides:** modeling an ontology, wiring an AI principal, writing a policy, building a plugin, bridging to LinkML/RDF.
- **API reference** auto-generated from docstrings (mkdocstrings).
- **Cookbook** mirroring the use-case doc (org brain, agent fleet, living review).
- Examples in `examples/` are run in CI so they never rot.

---

## 11. Metrics for success

Three layers. Engineering-health and product metrics are illustrative targets to calibrate, not contractual.

### 11.1 Engineering health (continuous)

| Metric | Target |
|---|---|
| CI pass rate on `main` | > 95% |
| Median PR review time | < 1 business day |
| Test coverage (domain / overall) | ≥ 90% / ≥ 85% |
| Flaky test rate | < 1% of runs |
| Defect escape rate (bugs found post-release) | trending down |
| CI wall-clock (PR pipeline) | < 10 min |
| Type coverage (public API) | 100% |
| Open contradictions in the *conformance* fixtures resolved | 100% before release |

### 11.2 Per-milestone acceptance (gates)

Each milestone ships only when its §2 exit criteria pass. The non-negotiables: **M2 cannot ship without full §19 conformance and a test proving no MCP write tool; M4 cannot ship without meeting the §9 perf budgets and a passing security review.**

### 11.3 Product & adoption (post-release; from PRD §14, with targets)

| Metric | Why it matters | Starting target |
|---|---|---|
| **Time-to-first-ontology** | core DX promise | < 10 min |
| Installs / KBs created | adoption | track curve |
| **Proposal acceptance rate** | collaboration health | establish baseline, watch drift |
| **Human-vs-AI authorship ratio** | is it actually co-authored? | both non-trivial |
| Contradiction open→resolve time | governance is working | resolves don't pile up |
| Reversal rate of AI-authored facts | trust calibration | low & stable |
| Grounding accuracy (downstream agents) | the payoff | beats no-Ontolith baseline |
| Third-party plugins / bridges in use | ecosystem | growing |

---

## 12. Risk register (execution)

| Risk | Signal | Mitigation |
|---|---|---|
| Bitemporal/conflict bugs | property tests falsify | front-loaded in M1–M2; 100% coverage on those modules; conformance kit |
| Traversal perf on SQLite default | benchmark exceeds budget | baseline early; budget gate by M4; scale-out adapter behind the port |
| `sqlite-vec` pre-v1 churn | minor bump breaks build | pinned, isolated behind the port, swap-ready |
| Dependency rule erosion | import-linter fails | CI gate from M0 |
| Plugin security holes | bandit/audit/sandbox review | deny-by-default; hardening by 1.0; signed registry |
| Scope creep into vNext (CRDT, UI) | M2/M3 slipping | explicit non-goals; RFC required to pull anything forward |
| Open-core boundary blur | proprietary value leaking into OSS, or vice-versa | the plugin ports *are* the boundary; review license placement per PR |

---

## 13. First two weeks (concrete kickoff)

1. Stand up the repo skeleton (§3.3), `pyproject` with extras, pre-commit, and the four CI workflows — all green on an empty package (**M0 gate**).
2. Wire `import-linter`, `Clock`/`IdProvider` ports, and the error taxonomy.
3. Record ADR-0001…0008 (the PRD decisions) + 0009–0011 (monorepo, toolchain, hexagonal).
4. Scaffold the `conformance/` kit with the first failing vector (supersession of a `time_varying` relation) to drive M1 toward M2.
5. Define the performance benchmark harness so M1 produces a baseline, not a guess.

---

## 14. Open execution questions

1. ~~Second backend for the conformance kit — DuckDB+DuckPGQ vs a maintained graph engine; pick by M3 against the traversal benchmark.~~ **Resolved:** DuckDB chosen (ADR-0016). DuckPGQ/graph-native traversal deferred — no traversal method exists on `StorageBackend` yet for it to serve.
2. ~~**MCP SDK choice & version** for the server surface.~~ **Resolved:** the official `mcp` Python SDK's `mcp.server.fastmcp.FastMCP` (`interfaces/mcp.py`), version-capped `>=1.28.1,<2.0` (ADR-0008, ADR-0014).
3. ~~**GraphQL library** (e.g. Strawberry) — confirm at M3.~~ **Resolved:** `strawberry-graphql` (ADR-0037).
4. **CLA vs DCO** — decide before the first external contribution (affects relicensing flexibility, PRD §12).
5. **Benchmark dataset** — synthetic generator parameters that resemble real org/research graphs.
6. ~~**Public API stability line** — exactly which symbols are covered by the SemVer guarantee at 1.0.~~ **Resolved:** ADR-0019 (M3) defined the surface — the union of eleven packages' `__all__` plus their exported classes' documented public methods — and M4 Workstream 5 (2026-09-21) audited it end to end and declared it the 1.0 freeze candidate; see ADR-0019's own Update section.
