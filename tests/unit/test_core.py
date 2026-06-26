"""Unit tests for core modules."""

from datetime import UTC, datetime

import pytest

from ontolith.core import (
    FixedClock,
    FixedIdProvider,
    OntolithError,
    SequentialIdProvider,
    SystemClock,
    UlidProvider,
)


class TestSystemClock:
    """Tests for SystemClock."""

    def test_returns_utc_time(self) -> None:
        """SystemClock should return UTC time."""
        clock = SystemClock()
        now = clock.now()

        assert now.tzinfo == UTC
        assert isinstance(now, datetime)


class TestFixedClock:
    """Tests for FixedClock."""

    def test_returns_fixed_time(self) -> None:
        """FixedClock should return the configured time."""
        clock = FixedClock("2025-01-01T00:00:00Z")

        assert clock.now() == datetime(2025, 1, 1, 0, 0, tzinfo=UTC)

    def test_advance_moves_time_forward(self) -> None:
        """advance() should move the clock forward."""
        clock = FixedClock("2025-01-01T00:00:00Z")
        clock.advance(days=1, hours=2, minutes=30)

        expected = datetime(2025, 1, 2, 2, 30, tzinfo=UTC)
        assert clock.now() == expected

    def test_set_changes_time(self) -> None:
        """set() should change the clock to a new time."""
        clock = FixedClock("2025-01-01T00:00:00Z")
        clock.set("2025-06-15T12:30:00Z")

        assert clock.now() == datetime(2025, 6, 15, 12, 30, tzinfo=UTC)

    def test_init_with_none_uses_current_time(self) -> None:
        """FixedClock(None) uses current UTC time."""
        before = datetime.now(UTC)
        clock = FixedClock(None)
        after = datetime.now(UTC)

        assert before <= clock.now() <= after
        assert clock.now().tzinfo == UTC

    def test_init_with_datetime_object(self) -> None:
        """FixedClock accepts a datetime object directly."""
        t = datetime(2025, 3, 15, 9, 0, tzinfo=UTC)
        clock = FixedClock(t)

        assert clock.now() == t

    def test_init_with_naive_datetime_adds_utc(self) -> None:
        """FixedClock adds UTC tzinfo to naive datetimes."""
        naive = datetime(2025, 3, 15, 9, 0)
        clock = FixedClock(naive)

        assert clock.now().tzinfo == UTC
        assert clock.now().replace(tzinfo=None) == naive

    def test_set_with_datetime_object(self) -> None:
        """set() accepts a datetime object."""
        clock = FixedClock("2025-01-01T00:00:00Z")
        t = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
        clock.set(t)

        assert clock.now() == t

    def test_set_with_naive_datetime_adds_utc(self) -> None:
        """set() adds UTC tzinfo when given a naive datetime."""
        clock = FixedClock("2025-01-01T00:00:00Z")
        naive = datetime(2025, 6, 1, 12, 0)
        clock.set(naive)

        assert clock.now().tzinfo == UTC
        assert clock.now().replace(tzinfo=None) == naive


class TestUlidProvider:
    """Tests for UlidProvider."""

    def test_generates_unique_ids(self) -> None:
        """UlidProvider should generate unique IDs."""
        provider = UlidProvider()

        id1 = provider.next()
        id2 = provider.next()

        assert id1 != id2
        assert len(id1) == 26  # ULID length
        assert len(id2) == 26


class TestSequentialIdProvider:
    """Tests for SequentialIdProvider."""

    def test_generates_sequential_ids(self) -> None:
        """SequentialIdProvider should generate sequential IDs."""
        provider = SequentialIdProvider(prefix="test", start=1)

        assert provider.next() == "test-001"
        assert provider.next() == "test-002"
        assert provider.next() == "test-003"

    def test_reset_resets_counter(self) -> None:
        """reset() should reset the counter."""
        provider = SequentialIdProvider(prefix="test", start=1)

        provider.next()
        provider.next()
        provider.reset(start=10)

        assert provider.next() == "test-010"


class TestFixedIdProvider:
    """Tests for FixedIdProvider."""

    def test_cycles_through_ids(self) -> None:
        """FixedIdProvider should cycle through the ID list."""
        provider = FixedIdProvider(["alice", "bob", "charlie"])

        assert provider.next() == "alice"
        assert provider.next() == "bob"
        assert provider.next() == "charlie"
        assert provider.next() == "alice"  # cycles back

    def test_reset_returns_to_first(self) -> None:
        """reset() should return to the first ID."""
        provider = FixedIdProvider(["alice", "bob"])

        provider.next()
        provider.next()
        provider.reset()

        assert provider.next() == "alice"

    def test_requires_at_least_one_id(self) -> None:
        """FixedIdProvider should require at least one ID."""
        with pytest.raises(ValueError, match="at least one ID"):
            FixedIdProvider([])


class TestErrors:
    """Tests for error taxonomy."""

    def test_ontolith_error_has_code_and_message(self) -> None:
        """OntolithError should have code and message."""
        error = OntolithError("test message", detail={"key": "value"})

        assert error.code == "ONTOLITH_ERROR"
        assert error.message == "test message"
        assert error.detail == {"key": "value"}

    def test_error_detail_defaults_to_empty_dict(self) -> None:
        """Error detail should default to empty dict."""
        error = OntolithError("test")

        assert error.detail == {}
