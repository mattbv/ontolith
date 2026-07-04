"""Property-based tests for SPEC §10 conflict routing (govern/conflict.py).

route() is a pure function — no I/O, no storage — so these tests exercise it
directly with Hypothesis-generated assertions rather than going through a KB.
Covers the invariants CLAUDE.md calls out as first-class for property testing:
conflict routing correctness and determinism.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from ontolith.core import Assertion
from ontolith.govern.conflict import Activate, Contradict, Supersede, _windows_overlap, route

T0 = datetime(2025, 1, 1, tzinfo=UTC)
AUTHOR = "alice@example.com"


def _assertion(
    id: str,
    value: str,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> Assertion:
    return Assertion(
        id=id,
        namespace="default",
        subject="entity-1",
        predicate="Person.name",
        value_kind="literal",
        value_type="Text",
        value=value,
        author=AUTHOR,
        asserted_at=T0,
        valid_from=valid_from,
        valid_to=valid_to,
        status="active",
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_short_values = st.text(
    min_size=1, max_size=5, alphabet=st.characters(whitelist_categories=("Lu", "Ll"))
)
_day_offsets = st.one_of(st.none(), st.integers(min_value=0, max_value=60))


def _offset_to_datetime(offset: int | None) -> datetime | None:
    return T0 + timedelta(days=offset) if offset is not None else None


@st.composite
def existing_and_incoming(
    draw: st.DrawFn,
) -> tuple[list[Assertion], Assertion]:
    """Generate a list of existing assertions plus one incoming assertion,
    all sharing (subject, predicate) but with varied values and windows.
    """
    n = draw(st.integers(min_value=0, max_value=6))
    existing = []
    for i in range(n):
        value = draw(_short_values)
        vf = _offset_to_datetime(draw(_day_offsets))
        vt = _offset_to_datetime(draw(_day_offsets))
        existing.append(_assertion(f"e-{i}", value, valid_from=vf, valid_to=vt))

    incoming_value = draw(_short_values)
    incoming_vf = _offset_to_datetime(draw(_day_offsets))
    incoming_vt = _offset_to_datetime(draw(_day_offsets))
    incoming = _assertion("incoming", incoming_value, valid_from=incoming_vf, valid_to=incoming_vt)
    return existing, incoming


# ===========================================================================
# route() — empty existing always activates
# ===========================================================================


@given(incoming_value=_short_values)
@settings(max_examples=50)
def test_empty_existing_always_activates(incoming_value: str) -> None:
    """route() with no existing assertions always returns Activate, for either temporality."""
    incoming = _assertion("incoming", incoming_value)
    assert route(incoming, [], "static") == Activate()
    assert route(incoming, [], "time_varying") == Activate()


# ===========================================================================
# route() — determinism (pure function property)
# ===========================================================================


@given(
    data=existing_and_incoming(),
    temporality=st.sampled_from(["static", "time_varying"]),
)
@settings(max_examples=100)
def test_route_is_deterministic(
    data: tuple[list[Assertion], Assertion],
    temporality: str,
) -> None:
    """Calling route() twice with identical inputs always yields an equal result."""
    existing, incoming = data
    result1 = route(incoming, existing, temporality)  # type: ignore[arg-type]
    result2 = route(incoming, existing, temporality)  # type: ignore[arg-type]
    assert result1 == result2


# ===========================================================================
# Static routing — contradiction iff any value differs
# ===========================================================================


@given(data=existing_and_incoming())
@settings(max_examples=100)
def test_static_routing_contradicts_iff_value_differs(
    data: tuple[list[Assertion], Assertion],
) -> None:
    """SPEC §10.3: static routing ignores validity windows entirely — only value
    equality matters. Contradict iff at least one existing value differs.
    """
    existing, incoming = data
    result = route(incoming, existing, "static")

    conflicting_ids = {e.id for e in existing if e.value != incoming.value}

    if not conflicting_ids:
        assert result == Activate()
    else:
        assert isinstance(result, Contradict)
        assert set(result.member_ids) == conflicting_ids | {incoming.id}
        # incoming is always last, conflicting existing members precede it
        assert result.member_ids[-1] == incoming.id


# ===========================================================================
# Time-varying routing — supersede iff window overlaps AND value differs
# ===========================================================================


@given(data=existing_and_incoming())
@settings(max_examples=100)
def test_time_varying_supersedes_iff_overlap_and_value_differs(
    data: tuple[list[Assertion], Assertion],
) -> None:
    """SPEC §10.2: time_varying only supersedes assertions whose window overlaps
    the incoming one AND whose value differs. Non-overlapping or same-value
    assertions are left untouched (coexistence).
    """
    existing, incoming = data
    result = route(incoming, existing, "time_varying")

    expected_targets = {
        e.id for e in existing if _windows_overlap(e, incoming) and e.value != incoming.value
    }

    if not expected_targets:
        assert result == Activate()
    else:
        assert isinstance(result, Supersede)
        assert set(result.targets) == expected_targets


# ===========================================================================
# _windows_overlap — geometric properties
# ===========================================================================


@given(
    a_from=_day_offsets,
    a_to=_day_offsets,
    b_from=_day_offsets,
    b_to=_day_offsets,
)
@settings(max_examples=100)
def test_windows_overlap_is_symmetric(
    a_from: int | None, a_to: int | None, b_from: int | None, b_to: int | None
) -> None:
    """Overlap is a symmetric relation: overlap(a, b) == overlap(b, a)."""
    a = _assertion(
        "a", "x", valid_from=_offset_to_datetime(a_from), valid_to=_offset_to_datetime(a_to)
    )
    b = _assertion(
        "b", "y", valid_from=_offset_to_datetime(b_from), valid_to=_offset_to_datetime(b_to)
    )
    assert _windows_overlap(a, b) == _windows_overlap(b, a)


@given(gap_days=st.integers(min_value=1, max_value=30))
@settings(max_examples=30)
def test_touching_windows_do_not_overlap(gap_days: int) -> None:
    """Half-open interval convention: [s1, e1) and [e1, e2) do not overlap
    even though they touch at the boundary.
    """
    boundary = T0 + timedelta(days=gap_days)
    a = _assertion("a", "x", valid_from=T0, valid_to=boundary)
    b = _assertion("b", "y", valid_from=boundary, valid_to=None)
    assert _windows_overlap(a, b) is False
    assert _windows_overlap(b, a) is False


@given(
    a_from=st.integers(min_value=0, max_value=30),
    a_to=st.integers(min_value=31, max_value=60),
)
@settings(max_examples=30)
def test_identical_windows_overlap(a_from: int, a_to: int) -> None:
    """A window always overlaps an identical copy of itself."""
    vf, vt = _offset_to_datetime(a_from), _offset_to_datetime(a_to)
    a = _assertion("a", "x", valid_from=vf, valid_to=vt)
    b = _assertion("b", "y", valid_from=vf, valid_to=vt)
    assert _windows_overlap(a, b) is True


@given(offset=st.integers(min_value=0, max_value=60))
@settings(max_examples=30)
def test_fully_open_windows_always_overlap(offset: int) -> None:
    """Two windows with no valid_from/valid_to bounds always overlap (both span [epoch, ∞))."""
    a = _assertion("a", "x")
    b = _assertion("b", "y", valid_from=T0 + timedelta(days=offset))
    assert _windows_overlap(a, b) is True
