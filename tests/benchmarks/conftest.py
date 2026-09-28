"""Shared helper for the M4 Workstream 1 SPEC §9 performance-budget gate.

`assert_within_budget` enforces a budget row as a hard ceiling on the
*slowest observed round* (`benchmark.stats.stats.max`), not a literal p95
percentile. pytest-benchmark's own `Stats` class has no p95 field (only
min/max/mean/median/stddev/iqr/q1/q3 — checked directly against the
installed `pytest_benchmark.stats.Stats.fields`), and computing one by hand
from `.data` would be unreliable for the slower budget rows, which
pytest-benchmark calibrates to a handful of rounds. `max < budget` implies
`p95 < budget` for any distribution (p95 can never exceed the maximum), so
this is a strictly *more* conservative check than the literal SPEC §9
wording — never a weaker one — and needs no percentile machinery at all.

Not a relative "no > 15% regression vs baseline" check (the Implementation
Plan's Quality Gates table originally sketched that mechanism) — no baseline
is stored or compared against. An absolute per-SPEC-§9-row ceiling was
chosen instead: it directly enforces the actual named M4 exit criterion
("performance budgets met (§9)"), needs no committed baseline file (`ci.yml`
deliberately holds only `contents: read`, a Workstream 7 security-review
fix — a job that updates a committed baseline would need write access),
and avoids the CI-runner-speed-sensitivity flakiness this project has
already hit twice (`tests/conformance/test_bitemporal.py`'s Hypothesis
deadline, `TestResolverConcurrency`'s wall-clock assertion — both fixed in
PR #138) — the existing §9 margins are wide enough (three to four orders of
magnitude for traversal, the tightest row still comfortably double digits to
low hundreds of times under budget) that a relative regression-vs-baseline
check would rarely be the thing that actually catches a real regression
before the absolute ceiling does, while adding real machinery and its own
new flake surface.
"""

from __future__ import annotations

from typing import Any


def assert_within_budget(benchmark: Any, budget_seconds: float, *, label: str) -> None:
    """Fail with a clear message if the slowest observed round exceeds budget.

    `label` should name the SPEC §9 row being enforced, e.g.
    "3-hop traversal, 100k-assertion KB (p95 < 200 ms)".
    """
    observed = benchmark.stats.stats.max
    assert observed < budget_seconds, (
        f"{label}: slowest round {observed * 1000:.2f} ms exceeds the "
        f"{budget_seconds * 1000:.0f} ms SPEC §9 budget "
        f"(mean {benchmark.stats.stats.mean * 1000:.2f} ms over "
        f"{benchmark.stats.stats.rounds} rounds)"
    )
