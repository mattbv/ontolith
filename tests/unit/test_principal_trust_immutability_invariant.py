"""Guards the invariant KI-036's `.trust_at_least()`/`.as_of()` fix depends on.

`entities_meeting_trust` (both backends) treats a principal's `trust_level`
as always-current rather than bitemporally reconstructed, on the grounds
that no code path ever mutates it after principal creation - so "trust_level
as of any t" and "trust_level now" are provably the same value (see
`store/base.py`'s `entities_meeting_trust` docstring, KI-036). This is a
static, whole-codebase claim that a normal behavioral test can't pin down:
nothing here exercises a mutation path, because none exists. If someone
later adds one (e.g. `Ontology.set_trust_level()`), this is the test that
should start failing, as a prompt to version principals before relying on
`.trust_at_least()` + `.as_of()` again.
"""

from __future__ import annotations

import re
from pathlib import Path

_BACKEND_FILES = [
    Path(__file__).parents[2] / "src" / "ontolith" / "store" / "sqlite" / "backend.py",
    Path(__file__).parents[2] / "src" / "ontolith" / "store" / "duckdb" / "backend.py",
]

# Matches "UPDATE principal"/"DELETE FROM principal" but not the
# principal_credential table (a trailing "_" rules that out).
_MUTATES_PRINCIPAL = re.compile(r"\b(UPDATE|DELETE\s+FROM)\s+principal(?!_)\b")


def test_no_backend_statement_mutates_the_principal_table() -> None:
    for path in _BACKEND_FILES:
        source = path.read_text()
        match = _MUTATES_PRINCIPAL.search(source)
        assert match is None, (
            f"{path} contains {match.group(0) if match else ''!r} - a write path onto the "
            "principal table now exists. entities_meeting_trust()'s as_of_time handling "
            "(KI-036) assumes trust_level is immutable after creation; if this is a "
            "trust_level mutation, .trust_at_least() + .as_of() needs a real bitemporal "
            "reconstruction (principal versioning) before this assumption holds again."
        )
