"""Unit tests for ontolith.govern.contradiction.safe_rationale_history (KI-075).

`Contradiction.metadata` is an open, schema-less blob (ADR-0041) — nothing
enforces that `rationale_history` is a list, that its entries are dicts, or
that those dicts carry all three keys with string values. This function is
the one place every read surface that projects individual entries out of it
(GraphQL's `ContradictionType`, the CLI's `contradiction` sub-app) defends
against a malformed blob (KI-075 review, round 2 — a per-field `.get(key,
default)` at each call site still raised on a non-dict entry, a non-list
`rationale_history`, or a present-but-`None` value).
"""

from ontolith.govern.contradiction import safe_rationale_history


class TestSafeRationaleHistory:
    def test_missing_key_returns_empty_list(self) -> None:
        assert safe_rationale_history({}) == []

    def test_well_formed_entries_pass_through_unchanged(self) -> None:
        metadata = {
            "rationale_history": [
                {"rationale": "a", "actor": "alice@example.com", "at": "2025-01-01T00:00:00+00:00"},
                {"rationale": "b", "actor": "bob@example.com", "at": "2025-01-02T00:00:00+00:00"},
            ]
        }
        assert safe_rationale_history(metadata) == metadata["rationale_history"]

    def test_non_list_rationale_history_returns_empty_list(self) -> None:
        """A domain-side corruption (e.g. KI-071's own accumulation logic
        misapplied to a non-list value) must not propagate into malformed
        per-character "entries" on the read side."""
        assert safe_rationale_history({"rationale_history": "not a list"}) == []

    def test_non_dict_entry_is_skipped_not_raised(self) -> None:
        metadata = {
            "rationale_history": [
                "a bare string",
                42,
                None,
                {"rationale": "well-formed", "actor": "alice@example.com", "at": "2025-01-01"},
            ]
        }
        assert safe_rationale_history(metadata) == [
            {"rationale": "well-formed", "actor": "alice@example.com", "at": "2025-01-01"}
        ]

    def test_missing_fields_default_to_empty_string(self) -> None:
        assert safe_rationale_history({"rationale_history": [{"rationale": "only this"}]}) == [
            {"rationale": "only this", "actor": "", "at": ""}
        ]

    def test_present_but_none_fields_default_to_empty_string_not_none(self) -> None:
        """A plain `.get(key, default)` does NOT catch this: the key IS
        present, just holds `None`, so the default is never used — this is
        the gap round 2 review found `.get()` alone left open (GraphQL's
        `actor`/`at`/`rationale` fields are non-nullable, so a raw `None`
        there fails the whole containing query, not just this entry)."""
        metadata = {"rationale_history": [{"rationale": None, "actor": None, "at": None}]}
        assert safe_rationale_history(metadata) == [{"rationale": "", "actor": "", "at": ""}]

    def test_non_string_field_values_are_coerced_to_string(self) -> None:
        metadata = {"rationale_history": [{"rationale": 123, "actor": True, "at": 5.0}]}
        assert safe_rationale_history(metadata) == [
            {"rationale": "123", "actor": "True", "at": "5.0"}
        ]
