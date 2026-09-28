"""Shared helper for the M4 Workstream 1 SPEC §9 performance-budget gate.

`assert_within_budget` enforces a budget row as a hard ceiling on the
*95th-percentile* round duration, computed from pytest-benchmark's own raw
per-round data (`benchmark.stats.stats.data`) via `statistics.quantiles`.
pytest-benchmark's `Stats` class has no p95 field of its own (only
min/max/mean/median/stddev/iqr/q1/q3 — checked directly against the
installed `pytest_benchmark.stats.Stats.fields`), but it does keep every
individual round's duration, and every one of the 5 gated rows collects
hundreds to several thousand rounds on both a real CI runner and locally
(confirmed directly from downloaded CI artifacts, not assumed) — enough for
a real percentile, not an unreliable one.

**This file previously gated on `benchmark.stats.stats.max` instead**,
reasoning that `max < budget` is a strictly more conservative implication of
`p95 < budget` (true) and that pytest-benchmark "calibrates to a handful of
rounds" for the slower budget rows (false — see above). That version was
flaky in practice: a real CI run of `test_bench_propose_auto_accept` (budget
50 ms) recorded a `max` of 53.6 ms from a single stalled round (likely
SQLite fsync/WAL-checkpoint contention on a shared runner, though nothing in
the data confirms the exact cause — only that it was one isolated outlier
round out of 1,418), while the SAME run's real p95 was 0.62 ms — about 80x
under budget. `max` is maximally sensitive to exactly one outlier round out
of a thousand-plus; that's a materially *worse* choice for CI-runner-noise
robustness than a genuine percentile, not a safer one, despite being a
logically stronger bound on paper. Found by review, independently
reproduced against the real CI artifact before fixing (see the CHANGELOG
and Implementation Plan §9's own record for the full numbers).

Not a relative "no > 15% regression vs baseline" check (the Implementation
Plan's Quality Gates table originally sketched that mechanism) — no baseline
is stored or compared against. An absolute per-SPEC-§9-row ceiling was
chosen instead because it directly enforces the actual named M4 exit
criterion ("performance budgets met (§9)"), and needs no committed baseline
file (`ci.yml` deliberately holds only `contents: read`, a Workstream 7
security-review fix). This is a real, disclosed trade-off, not a strictly
better choice: at today's margins (measured directly across 6 real CI runs:
roughly 60x under budget on the tightest observed row/run down to nearly
3,000x on the loosest — the hybrid-query and propose rows are consistently
the tightest of the 5, trading places with each other run to run, not one
row always beating the other) a relative 15% regression check would catch a
real slowdown far earlier than an absolute ceiling this loose ever would —
even the tightest observed margin is well over an order of magnitude. A
relative check was not ruled out by any hard technical blocker (it could
read a prior run's artifact via `actions/download-artifact` without needing
repo write access); it was deliberately left for later, later-workstream
scope, to close the named exit criterion now without introducing a second
new mechanism at the same time.
"""

from __future__ import annotations

import statistics
from typing import Any


def assert_within_budget(benchmark: Any, budget_seconds: float, *, label: str) -> None:
    """Fail with a clear message if the row's p95 duration exceeds budget.

    `label` should name the SPEC §9 row being enforced, e.g.
    "3-hop traversal, 100k-assertion KB (p95 < 200 ms)".

    No-ops (rather than raising `AttributeError`) if `benchmark.stats` is
    `None` — the shape it takes under `--benchmark-disable`, a mode nothing
    in this project's CI uses today, but one a developer might reach for
    locally when running the benchmark's own assertions as a plain test.
    """
    if benchmark.stats is None:
        return
    data = benchmark.stats.stats.data
    p95 = statistics.quantiles(data, n=100)[94] if len(data) >= 2 else data[0]
    assert p95 < budget_seconds, (
        f"{label}: p95 {p95 * 1000:.2f} ms exceeds the "
        f"{budget_seconds * 1000:.0f} ms SPEC §9 budget "
        f"(mean {benchmark.stats.stats.mean * 1000:.2f} ms, "
        f"max {benchmark.stats.stats.max * 1000:.2f} ms, over "
        f"{benchmark.stats.stats.rounds} rounds)"
    )
