#!/usr/bin/env python3
"""Bitemporal queries & time-travel example.

This example demonstrates:
- `time_varying` properties: supersession instead of contradiction
  (SPEC §10.2) -- a changing value over time is expected, not disputed
- The TWO time dimensions bitemporality tracks: valid time (when a fact
  was true in the real world) and assertion time (when we learned it) --
  using an injected `FixedClock` to control the latter deterministically
  (this project never calls `datetime.now()` directly in domain code)
- as_of(t): reconstructing what the KB looked like at a point in time
- Why as_of() can return NOTHING for a real historical fact, if we
  hadn't recorded it yet at time t
"""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ontolith import Ontology
from ontolith.core import FixedClock
from ontolith.schema import Concept, Property, Text, compile_schema


def main() -> None:
    """Run the bitemporal queries example."""
    db_path = Path(tempfile.mktemp(suffix=".db"))
    print("⏳ Ontolith Bitemporal Queries Example\n")

    # An injected, controllable clock -- asserted_at is stamped from this,
    # never from wall-clock time (CLAUDE.md's determinism-by-construction
    # rule). Starting "now" at 2023-01-01.
    clock = FixedClock("2023-01-01T00:00:00Z")
    kb = Ontology.connect(db_path, clock=clock)
    hr = kb.create_principal("hr@example.com", kind="human", default_capability="admin")

    class Person(Concept):
        name: Text
        # A job title changes over time -- that's expected, not a
        # contradiction between sources. Declaring it time_varying routes
        # a differing value to supersession instead (SPEC §10, ADR-0017).
        title: Text = Property(temporality="time_varying")

    schema = compile_schema("default", 1, Person)
    kb.apply_schema(schema, author=hr.id)

    dana = kb.create_entity(concept="Person", author=hr.id, natural_key="dana")
    kb.assert_literal(
        subject=dana.id, predicate="Person.name", value="Dana", value_type="Text", author=hr.id
    )

    # Recorded on 2023-01-01 (the clock's current time = asserted_at):
    # Dana has been "Engineer" since 2020, with no end date yet -- an
    # open-ended window.
    kb.assert_literal(
        subject=dana.id,
        predicate="Person.title",
        value="Engineer",
        value_type="Text",
        author=hr.id,
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
    )

    # Time passes in the real world; the clock advances.
    clock.advance(days=150)  # now 2023-05-31

    # Dana got promoted on 2023-06-01. This assertion's window OVERLAPS
    # the still-open "Engineer" window and has a different value --
    # SPEC §10.2 supersession closes the prior window automatically
    # (valid_to = this assertion's valid_from) rather than contradicting.
    kb.assert_literal(
        subject=dana.id,
        predicate="Person.title",
        value="Senior Engineer",
        value_type="Text",
        author=hr.id,
        valid_from=datetime(2023, 6, 1, tzinfo=UTC),
    )

    print("Current title (status=active only, the default):")
    for a in kb.assertions(subject=dana.id, predicate="Person.title"):
        print(f"  {a.value!r} (valid_from={a.valid_from.date()})")
    print()

    print("Full history (status=None -- superseded assertions are kept, never deleted):")
    for a in kb.assertions(subject=dana.id, predicate="Person.title", status=None):
        vt = a.valid_to.date() if a.valid_to else "open"
        print(f"  {a.value!r} — {a.status} (valid {a.valid_from.date()} to {vt})")
    print()

    # --- Time travel ---
    print("as_of(2023-03-01) -- after the Engineer assertion, before the promotion:")
    for a in kb.as_of(datetime(2023, 3, 1, tzinfo=UTC)).assertions(
        subject=dana.id, predicate="Person.title"
    ):
        print(f"  {a.value!r}")

    print("as_of(2023-07-01) -- after the promotion:")
    for a in kb.as_of(datetime(2023, 7, 1, tzinfo=UTC)).assertions(
        subject=dana.id, predicate="Person.title"
    ):
        print(f"  {a.value!r}")

    # This is the subtle part: Dana's title was ALSO "Engineer" in the
    # real world back in 2021 -- but we didn't record that fact until
    # 2023-01-01. as_of() answers "what did we KNOW at t", not "what was
    # true at t" -- both valid_from<=t AND asserted_at<=t must hold.
    print("as_of(2021-06-01) -- a real historical date, but BEFORE we ever recorded anything:")
    titles = [
        a.value
        for a in kb.as_of(datetime(2021, 6, 1, tzinfo=UTC)).assertions(
            subject=dana.id, predicate="Person.title"
        )
    ]
    print(f"  {titles!r} (empty -- we hadn't asserted anything yet as of this date)")

    kb.close()
    db_path.unlink()
    print("\n✅ Bitemporal queries example complete!")


if __name__ == "__main__":
    main()
