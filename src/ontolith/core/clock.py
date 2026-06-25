"""Clock abstraction for deterministic time handling.

The Clock port ensures bitemporal logic is reproducible in tests by injecting
time rather than using datetime.now() directly in domain logic.
"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta


class Clock(ABC):
    """Abstract clock for deterministic time handling."""

    @abstractmethod
    def now(self) -> datetime:
        """Return the current time.

        Returns:
            Current datetime in UTC.
        """
        ...


class SystemClock(Clock):
    """Production clock using system time."""

    def now(self) -> datetime:
        """Return the current system time in UTC.

        Returns:
            Current datetime in UTC timezone.
        """
        return datetime.now(UTC)


class FixedClock(Clock):
    """Test clock that returns a fixed time.

    Useful for testing time-sensitive logic without flakiness.

    Args:
        fixed_time: The time to always return, or current time if None.

    Example:
        >>> clock = FixedClock("2025-01-01T00:00:00Z")
        >>> clock.now()
        datetime.datetime(2025, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
        >>> clock.advance(days=1)
        >>> clock.now()
        datetime.datetime(2025, 1, 2, 0, 0, tzinfo=datetime.timezone.utc)
    """

    def __init__(self, fixed_time: str | datetime | None = None) -> None:
        """Initialize with a fixed time.

        Args:
            fixed_time: ISO-8601 string, datetime, or None for current time.
        """
        if fixed_time is None:
            self._time = datetime.now(UTC)
        elif isinstance(fixed_time, str):
            self._time = datetime.fromisoformat(fixed_time.replace("Z", "+00:00"))
        else:
            self._time = fixed_time

        # Ensure timezone-aware
        if self._time.tzinfo is None:
            self._time = self._time.replace(tzinfo=UTC)

    def now(self) -> datetime:
        """Return the fixed time.

        Returns:
            The configured fixed datetime.
        """
        return self._time

    def set(self, time: str | datetime) -> None:
        """Set the clock to a new time.

        Args:
            time: ISO-8601 string or datetime to set.
        """
        if isinstance(time, str):
            self._time = datetime.fromisoformat(time.replace("Z", "+00:00"))
        else:
            self._time = time

        if self._time.tzinfo is None:
            self._time = self._time.replace(tzinfo=UTC)

    def advance(
        self,
        *,
        days: int = 0,
        hours: int = 0,
        minutes: int = 0,
        seconds: int = 0,
    ) -> None:
        """Advance the clock by the specified duration.

        Args:
            days: Number of days to advance.
            hours: Number of hours to advance.
            minutes: Number of minutes to advance.
            seconds: Number of seconds to advance.
        """
        self._time += timedelta(
            days=days,
            hours=hours,
            minutes=minutes,
            seconds=seconds,
        )


__all__ = ["Clock", "SystemClock", "FixedClock"]
