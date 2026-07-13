"""Property-based tests for the SPEC §5 append-only assertion invariant.

SPEC §5.3 requires append-only as a first-class property-testing target:
no operation may mutate an assertion's `value` after creation, and nothing
is ever deleted — only `status`, `valid_to`, and `supersedes` may change.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import Clock, FixedClock, FixedIdProvider, IdProvider
from ontolith.store.duckdb import DuckDBBackend
from ontolith.store.sqlite import SQLiteBackend

T0 = datetime(2025, 1, 1, tzinfo=UTC)
AUTHOR = "alice@example.com"

_short_values = st.text(
    min_size=1, max_size=5, alphabet=st.characters(whitelist_categories=("Lu", "Ll"))
)


def _connect(backend_name: str, path: Path, clock: Clock, id_provider: IdProvider) -> Ontology:
    """Build an Ontology directly, not via the make_kb fixture: Hypothesis's
    function_scoped_fixture health check flags function-scoped fixtures used
    alongside @given (fixture setup runs once, but the test body runs once
    per example) - backend_name (a plain immutable string) is safe to use
    that way, but make_kb/tmp_path are not, so backends are built inline
    here instead, preserving the existing fresh-tempdir-per-example pattern.
    """
    backend_cls = SQLiteBackend if backend_name == "sqlite" else DuckDBBackend
    return Ontology(backend_cls(path, clock=clock), clock=clock, id_provider=id_provider)


# ===========================================================================
# Direct invariant: the frozen model rejects in-place mutation (KI-003)
# ===========================================================================


def test_assertion_value_mutation_raises(make_kb: KbFactory) -> None:
    """Attempting to mutate assertion.value in-place raises ValidationError (SPEC §5)."""
    clock = FixedClock(T0)
    ids = FixedIdProvider(["e-1", "a-1", "prop-1"])
    kb = make_kb(clock, ids)
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author=AUTHOR)
    proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
    assertion = kb.assertions(subject=entity.id, predicate="Person.name")[0]

    try:
        with pytest.raises(ValidationError):
            assertion.value = "tampered"  # type: ignore[misc]
    finally:
        kb.close()


def test_assertion_status_cannot_be_mutated_directly(make_kb: KbFactory) -> None:
    """Status transitions ARE allowed (SPEC §5), but only via the backend's
    set_assertion_status() — never by assigning to the frozen model in-place.
    """
    clock = FixedClock(T0)
    ids = FixedIdProvider(["e-1", "a-1", "prop-1"])
    kb = make_kb(clock, ids)
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    entity = kb.create_entity("Person", author=AUTHOR)
    kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
    assertion = kb.assertions(subject=entity.id, predicate="Person.name")[0]

    try:
        with pytest.raises(ValidationError):
            assertion.status = "retracted"  # type: ignore[misc]
    finally:
        kb.close()


# ===========================================================================
# Property: randomized propose() sequences never mutate a prior assertion's value
# ===========================================================================


@given(values=st.lists(_short_values, min_size=1, max_size=8))
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_propose_sequence_never_mutates_prior_assertion_values(
    values: list[str], backend_name: str
) -> None:
    """For any sequence of static-property proposals (triggering corroboration or
    contradiction), every assertion ever created keeps its original value forever.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        clock = FixedClock(T0)
        ids = FixedIdProvider([f"id-{i}" for i in range(len(values) * 3 + 10)])
        kb = _connect(backend_name, Path(tmpdir) / "prop.db", clock, ids)
        try:
            kb.create_principal(
                AUTHOR, kind="human", auth_method="oidc", default_capability="write"
            )
            entity = kb.create_entity("Person", author=AUTHOR)

            recorded: dict[str, str] = {}
            for value in values:
                kb.propose(entity.id, "Person.name", value, "Text", AUTHOR)
                all_assertions = kb.assertions(
                    subject=entity.id, predicate="Person.name", status=None
                )
                for a in all_assertions:
                    if a.id not in recorded:
                        recorded[a.id] = a.value

            final_assertions = kb.assertions(
                subject=entity.id, predicate="Person.name", status=None
            )
            final_by_id = {a.id: a.value for a in final_assertions}

            for assertion_id, original_value in recorded.items():
                assert final_by_id[assertion_id] == original_value
        finally:
            kb.close()


# ===========================================================================
# Property: total assertion count never decreases (nothing is ever deleted)
# ===========================================================================


@given(values=st.lists(_short_values, min_size=1, max_size=6))
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_assertion_count_never_decreases_across_propose_and_retract(
    values: list[str], backend_name: str
) -> None:
    """Interleave propose() and retract() calls; the total count of assertion
    records (status=None) must be monotonically non-decreasing — retraction
    changes status, it never deletes a row.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        clock = FixedClock(T0)
        ids = FixedIdProvider([f"id-{i}" for i in range(len(values) * 3 + 10)])
        kb = _connect(backend_name, Path(tmpdir) / "prop.db", clock, ids)
        try:
            kb.create_principal(
                AUTHOR, kind="human", auth_method="oidc", default_capability="write"
            )
            entity = kb.create_entity("Person", author=AUTHOR)

            previous_count = 0
            for i, value in enumerate(values):
                kb.propose(entity.id, "Person.name", value, "Text", AUTHOR)

                current_count = len(
                    kb.assertions(subject=entity.id, predicate="Person.name", status=None)
                )
                assert current_count >= previous_count
                previous_count = current_count

                # Every other iteration, retract one active assertion (if any)
                if i % 2 == 1:
                    active = kb.assertions(
                        subject=entity.id, predicate="Person.name", status="active"
                    )
                    if active:
                        kb.retract(active[0].id, AUTHOR)
                        count_after_retract = len(
                            kb.assertions(subject=entity.id, predicate="Person.name", status=None)
                        )
                        assert count_after_retract >= previous_count
                        previous_count = count_after_retract
        finally:
            kb.close()
