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
| **M4 — Production (1.0)** | govern at scale | Hardened policy engine, plugin sandboxing, perf budgets met, complete docs, migration tooling, SPEC §18 observability (ADR-0044) | Security review passed; **performance budgets met** (§9); SemVer 1.0 API freeze; on-disk `format_version` frozen | Not started |
| **vNext** | live commons | CRDT multi-writer, assisted conflict resolution, federation, UI, marketplace | per-feature specs | Not started |

**Critical path:** M0 → M1 (core+store) → M2 (conflict+bitemporal+MCP). M2 is where the product becomes itself; protect its timeline. REST/GraphQL and scale-out (M3) can parallelize once the ports are stable.

**Status as of 2026-08-27** (updated opportunistically, not on every change — treat as a snapshot, verify against `CHANGELOG.md`/`git log` for anything time-sensitive): M0–M2 fully complete. M3's three named exit criteria are all met, and its full scope column is now complete too: the RDF/OWL bridge (ADR-0036 — schema as OWL ontology plus active-assertion instance data, export only, `rdflib`-backed; no import direction yet) and the GraphQL interface (ADR-0037 — `strawberry`-backed, query/propose/review operations per SPEC §14.3's literal wording, deliberately narrower than REST's own extended write/admin surface) both shipped. `docs/known-issues.md`'s backlog is empty — KI-001 through KI-051 are all resolved (none blocking, all found during code review rather than reported). M4 has not started.

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
- **On-disk `format_version`** is independent of SDK version and frozen at 1.0; migrations are reversible or explicitly marked irreversible (SPEC §15).
- Conventional commits drive an automated CHANGELOG and version bump.

---

## 8. Security & supply chain

- **Authz everywhere** (SPEC §17): capability checks at every interface boundary; accountable-owner invariant enforced in code and DB.
- **Agent safety:** MCP read/propose/flag only; no write tool; `propose` stamps author + model + `acting_as`.
- **Plugin sandboxing:** capability manifests, deny-by-default for storage/network/filesystem; **process isolation in v1, wasm/subprocess hardening + signed registry plugins by 1.0**.
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
6. **Public API stability line** — exactly which symbols are covered by the SemVer guarantee at 1.0.
