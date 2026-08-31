# ADR-0026: Security CI-Hardening Pass (`security.yml`) and Public API Diff Gate

**Status:** Accepted

**Date:** 2026-07-29

**Deciders:** Ontolith Core Team

**Related:** Implementation Plan §5 (code quality gates), §7.1 (CI pipeline), `docs/known-issues.md`
KI-020

## Context

KI-020 tracked a specific gap — no `griffe diff` CI gate for public-API breaking changes — but
explicitly deferred fixing it in isolation: the Implementation Plan bundles that gate with a
broader `security.yml`/`nightly.yml`/`release.yml` CI-hardening pass (§7.1), none of which existed
yet (only `ci.yml` did), and standing up `griffe` alone risked picking tooling/config choices that
should be made together with the rest of that pass.

Verified directly, not assumed, before building anything:

- `bandit` and `pip-audit` were already dev dependencies (`pyproject.toml`'s Security section)
  but never actually run anywhere in CI — installed, wired nowhere.
- Running both against the tree as it stood before this ADR: **`pip-audit` found 2 real
  vulnerabilities** — `mcp==1.28.0` (PYSEC-2026-3483, fixed in 1.28.1) and `sqlite-vec==0.1.1`
  (PYSEC-2026-1938, fixed in 0.1.3). **`bandit` found 7 medium-severity findings**, all `B608`
  (SQL-injection-shaped), across two distinct false-positive shapes rather than one: 5 in
  `store/{sqlite,duckdb}/backend.py`'s vector-search code, where a table name is built via
  `f"vector_{scope}"` (4 sites) or an `IN (...)` placeholder-arity string built from a repeated
  literal `"?"` (1 site, never from external input) — `scope` is validated against the closed
  `VECTOR_SCOPES` frozenset (`store/base.py`) before every one of these call sites, which bandit's
  static heuristic can't see; and 2 in `entities_where()` (same two backends), where a
  `flagged_clause` local is always one of exactly two hardcoded string literals, never
  caller-controlled, interpolated into a query string — a different call site and a different
  variable, but bandit's heuristic flags any keyword-containing-string + variable concatenation
  regardless of the variable's actual provenance. None of the 7 are real injection risk. (Also 17
  low-severity `B101` (`assert_used`) findings — already below the "no unsuppressed mediums+"
  threshold, not gate-relevant.)
- Neither finding set was hypothetical or pre-existing-and-ignorable: wiring these gates as
  blocking *before* fixing them would have broken CI on the first run. Fixing them first is what
  makes "blocking from day one" honest rather than aspirational.
- `griffe`'s CLI ships as a separate PyPI distribution (`griffecli`) from the `griffe` library
  itself, confirmed still true. `griffe check <package> -a <ref>` does the diff-against-a-git-ref
  work directly — no separate "generate a baseline snapshot" step needed, simpler than KI-020's
  original text assumed. Confirmed empirically: run against a commit several PRs back, it
  correctly reported the real breaking changes made since then (the `PolicyStrategy.evaluate()`
  `kb` parameter addition, `Ontology.namespace`'s literal-to-constant change) — **and exits `1`**
  when it does, including for changes that aren't actually breaking (e.g. that same
  `Ontology.namespace` line: a constant extraction with an identical value still gets reported and
  still exits `1`). An earlier draft of this ADR claimed the opposite (exit `0` regardless), from a
  verification bug — piping the command through `head` before checking `$?` captures `head`'s exit
  code, not `griffe`'s. Caught in review, re-verified directly without the pipe. The step is
  informational only because of `continue-on-error: true` on the CI step itself (§Decision), not
  because of anything in `griffe`'s own behavior.

## Decision

**1. Dependency remediation, done first, as a precondition for making these gates blocking:**
bump `mcp` to `>=1.28.1,<2.0` (the upper bound avoids `uv lock`'s resolver otherwise jumping to an
unreviewed `2.0.0` major, confirmed to happen with an unbounded `>=1.28.1`) and `sqlite-vec`'s pin
to the fixed `0.1.3` (re-verified the `vec0` DELETE+INSERT workarounds from ADR-0020 still hold —
full suite green, `vector_search`/`vector_upsert` conformance vectors pass unchanged). Targeted
`# nosec B608` on each of the 7 flagged call sites, with a comment at each explaining *why*
(`scope`/`flagged_clause` provenance, not "trust me") rather than a blanket file-level or
rule-level suppression that would hide a genuine future B608 in the same file. `[tool.bandit]
skips = ["B101"]` added for report cleanliness only — that rule was never blocking.

**2. New `.github/workflows/security.yml`**, matching Implementation Plan §7.1's `security.yml
(PR + weekly): pip-audit · bandit · gitleaks · SBOM (cyclonedx)` line exactly: triggers on
`pull_request` and a weekly `schedule` (the latter catches a CVE disclosed against an
already-merged dependency, independent of PR activity). Two jobs: `scan` (pip-audit, bandit, and
CycloneDX SBOM generation via the new `cyclonedx-bom` dev dependency's `cyclonedx-py` CLI, sharing
one checkout/`uv sync` since none of the three depend on each other's output — this repo's Actions
minutes run out often enough that avoiding three redundant syncs is worth the minor loss of
per-tool job isolation) and `gitleaks` (via `gitleaks/gitleaks-action@v3` — this repo is private
under a personal account, not an organization; the action's license requirement is gated on
account type, not repository visibility, so no `GITLEAKS_LICENSE` is needed *because it's a
personal account*, not because it's public — it isn't). Workflow-level `permissions: contents:
read`; `GITLEAKS_ENABLE_COMMENTS: false` so the gitleaks job doesn't need `pull-requests: write`
(this repo's default `GITHUB_TOKEN` is read-only) — findings still surface via the job's own log
output. All blocking, now that findings are clean.

**3. `griffe diff` gate, in `ci.yml`'s existing `quality` job**, not `security.yml` — it's an
API-stability check, not a security scan, closer in spirit to that job's existing
`lint-imports`/`interrogate` steps than to anything in `security.yml`. New `griffecli` dev
dependency; one new step, run once (gated to the `python-version == '3.12'` matrix leg, matching
how the existing `quickstart example`/coverage-upload steps in the `test` job are already scoped
to a single leg to avoid triplicate output) comparing the current `ontolith` package against the
PR's base commit (`github.event.pull_request.base.sha`, falling back to `HEAD` — a no-op — on a
non-PR push run) via `griffe check ontolith -s src -a "$BASE_REF" -f github`. `-f github` emits
native GitHub Actions `::warning::` annotations, which surface inline on the PR diff. The step
itself carries `continue-on-error: true` — `griffe check` exits `1` on any detected change
(§Context), so without that flag this would be a hard-blocking gate, not the informational one
Implementation Plan §5's threshold calls for ("warn pre-1.0, block post-1.0"). Blocking post-1.0
is then a one-line removal of `continue-on-error`, not a redesign.

**4. Deliberately not built in this pass** (both still named in KI-020's resolution and here, so
neither reads as silently dropped):

- **`nightly.yml`**: Implementation Plan §7.1 says "full benchmarks → publish trend." `ci.yml`
  already has an informational `benchmarks` job on push to `main` (unchanged by this ADR) —  the
  gap is specifically a true nightly cadence plus a trend-history destination (a dashboard, a
  gh-pages page, a separate artifact store), which is a product/hosting decision, not a mechanical
  one, and needs to be made by whoever owns that decision, not assumed here.
- **`release.yml`**: needs PyPI Trusted Publishing configured on pypi.org (only the project's
  actual maintainer can register the project and set that up — it's not a repo-side change) and a
  docs site to deploy (`mkdocs.yml` doesn't exist yet). Not completable end-to-end from inside the
  repo.

## Rationale

**Why fix the findings before wiring the gates, instead of wiring them first and fixing in a
follow-up:** a CI gate that's blocking-but-already-broken on merge either gets force-merged around
(defeating the point) or blocks unrelated work until someone circles back — worse than not having
the gate yet. Fixing first means "blocking" in this ADR is a true statement on day one.

**Why per-site `# nosec` comments instead of a bandit config-level suppression for B608 in the
vector-search files:** a file- or rule-level suppression would also hide a *real* future B608 in
the same files (e.g. if a later change added a genuinely caller-influenced string into a query
there) — the whole point of "no unsuppressed mediums+" is that suppressions should be as narrow
and as explained as the false positive they're covering, not wider.

**Why `griffe check`'s own default (always exit 0) is trusted rather than adding
`continue-on-error: true` to the CI step:** verified empirically (§Context) rather than assumed —
adding a redundant CI-side escape hatch on top of a tool that's already warn-only by construction
would be defensive noise, not a real safety net.

## Consequences

**Positive:** CI now actually runs the security tools it already depended on. Two real,
previously-undetected vulnerabilities are fixed. KI-020 is closed with the gate it specifically
asked for. Any future public-API breaking change gets flagged inline on the PR that introduces it,
closing the gap ADR-0019's pinned-`__all__` test couldn't (signature-level breakage on an
already-exported symbol).

**Negative / follow-ups:** `nightly.yml` and `release.yml` remain unbuilt — tracked as named,
explicit follow-ups (above), not silently dropped. Every `uv run bandit -r src/ontolith -c
pyproject.toml` run logs a `WARNING nosec encountered (B608), but no failed test` line for each of
the seven suppressed sites — on every run, not intermittently — even though the overall run still
reports zero issues and exits `0`. Confirmed empirically to be a bandit reporting quirk (the
warning's line attribution doesn't match the suppression's actual effect), not a real gap: removing
any of the seven suppressions and re-running reliably reintroduces exactly that many real findings.
Noted here so a future reader doesn't mistake the warning for something broken.

**Update (2026-08-31, KI-065):** the `mcp` upper-bound convention this ADR established (`<2.0`,
"avoids an unreviewed major bump") was extended to `strawberry-graphql[fastapi]` (`<1.0`) and
`rdflib` (`<8.0`) — the two other M3-era dependencies that previously had none. Not extended to
every direct dependency (`pydantic`, `typer`, `python-ulid`, `fastapi`, `duckdb`, `uvicorn`,
`pyyaml` remain unbounded) — those are a separate, broader decision tracked as a follow-up, not
silently out of scope.

## References

- Implementation Plan §5 (quality gates table), §7.1 (CI pipeline)
- ADR-0001 (sqlite-vec pinned, "must pin version, monitor" — this bump is that monitoring)
- ADR-0019 (public API stability policy — this ADR's griffe gate is the CI enforcement ADR-0019's
  own Consequences section named as a known gap)
- ADR-0020 (hybrid retrieval — the `vec0` DELETE+INSERT workarounds re-verified against the
  `sqlite-vec` bump)
- `docs/known-issues.md` KI-020 (resolved by this ADR)
