"""Observability port (SPEC §18, ADR-0044).

`ObservabilitySink` is the single abstract port for metrics, events, and
structured logs — SPEC §18 groups these as one concern with three output
shapes, not three independent subsystems (ADR-0044's own rationale for why
this is one port, not three). Domain code (`core/`, `schema/`, `govern/`,
`query/`) depends only on this abstract port, exactly as it already does for
`Clock`/`IdProvider`/`StorageBackend` — concrete, I/O-performing sinks (an
OpenTelemetry-backed one, a metrics-service client, etc.) live in `observe/`
as adapters and are wired at the composition root.

`StdlibLoggingSink` is the production-safe default (mirrors `Clock`'s
`SystemClock`): it does real I/O (writing through Python's own `logging`
module), but stdlib `logging` is not a swappable adapter in the dependency
rule's sense — it is no different from `SystemClock` calling `datetime.now()`
directly. Keeping it here, not in `observe/`, means instrumentation calls
are always safe to make with no configuration at all, the same guarantee
`Ontology` already gives `clock`/`id_provider` by defaulting them when not
injected — nothing regresses to silence just because a caller didn't wire up
a real backend yet (no concrete backend exists for `record_metric`/
`record_event` as of KI-064/ADR-0044 — those are namespaced under
`ontolith.<domain>` loggers here, an M4 tier-(a)/tier-(b) stopgap, not the
tier-(c) metrics backend ADR-0044 explicitly defers to its own follow-up
ADR).

`NullObservabilitySink` and `RecordingObservabilitySink` are explicit
opt-outs: the former for a deployment that wants Ontolith to stay silent by
choice, the latter (mirrors `FixedClock`/`FixedIdProvider`) for tests that
need to assert exactly what was emitted.

M4 priority order (ADR-0044): (a) structured, correlated logs — this port
and its wiring into the four existing ad hoc `logging.getLogger()` call
sites; (b) the four named lifecycle events; (c) the seven-metric surface,
deferred to its own follow-up ADR for a concrete backend choice. This module
ships the full port shape decided by ADR-0044 up front so it never needs
re-litigating, even though only tier (a)'s `log()` method has a real call
site as of this module's own introduction.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod


class ObservabilitySink(ABC):
    """Abstract sink for structured logs, lifecycle events, and metrics
    (SPEC §18, ADR-0044).

    Correlation fields (`namespace`, `principal`, `acting_as`,
    `proposal_id`, ...) are carried, not re-derived: callers thread whatever
    is already in scope through as keyword fields on the call itself —
    this port has no context-propagation mechanism of its own (ADR-0044
    deliberately rejected a `contextvars`-based one).

    `govern/policy` never calls this port — SPEC's purity requirement for
    the policy engine has no observability carve-out. Instrumentation
    happens one layer up, in `Ontology`'s own orchestration and in
    `interfaces/*`'s request-level handling.
    """

    @abstractmethod
    def log(self, level: int, message: str, **fields: object) -> None:
        """Emit a structured, correlated log line.

        Args:
            level: A stdlib `logging` level (`logging.DEBUG`/`INFO`/
                `WARNING`/`ERROR`/`CRITICAL`), not a sink-specific scale —
                every sink is expected to understand these five.
            message: Human-readable message, already fully formed (no
                %-style interpolation placeholders — callers format before
                calling this method, since not every sink is stdlib
                `logging`-backed).
            **fields: Structured, correlated context (e.g. `namespace=`,
                `principal=`, `acting_as=`, `proposal_id=`, `code=`).
                `exc_info=<exception>` is a recognized-by-convention field
                for a genuinely unhandled exception a sink may want to
                capture a traceback for (`StdlibLoggingSink` does; a sink
                that doesn't understand it just reports it as a plain
                field, no different from any other).
        """
        ...

    @abstractmethod
    def record_event(self, kind: str, **fields: object) -> None:
        """Record one occurrence of a named lifecycle event (SPEC §18's
        four: proposal lifecycle, contradiction open/resolve, schema
        migration, plugin load).

        Args:
            kind: Event kind, e.g. "proposal.accepted", "contradiction.opened".
            **fields: Structured, correlated context for this occurrence.
        """
        ...

    @abstractmethod
    def record_metric(self, name: str, value: float, **tags: str) -> None:
        """Record one observation of a named metric (SPEC §18's seven).

        Args:
            name: Metric name, e.g. "proposals.accepted", "query.latency_ms".
            value: The observed value.
            **tags: Metric dimensions (e.g. `namespace=`, `backend=`) — kept
                as plain strings, unlike `log()`/`record_event()`'s
                arbitrary-object fields, since every metrics backend this
                port is likely to sit in front of treats tags/labels as
                strings.
        """
        ...


class StdlibLoggingSink(ObservabilitySink):
    """Production-safe default sink (mirrors `Clock`'s `SystemClock`):
    writes through Python's own `logging` module, under an
    `ontolith.observability` logger by default so a deployment can filter
    or route it independently of any other logger this project uses
    directly (e.g. a module still doing its own `logging.getLogger(__name__)`
    for something outside this port's scope).

    This is not the tier-(c) metrics backend ADR-0044 defers to its own
    follow-up ADR — `record_metric`/`record_event` land here as a visible,
    zero-configuration stopgap (a structured log line), not a real
    counter/gauge a monitoring stack could scrape. Swap in a concrete
    `observe/`-adapter sink once one exists for that.
    """

    def __init__(self, logger_name: str = "ontolith.observability") -> None:
        self._logger = logging.getLogger(logger_name)

    def log(self, level: int, message: str, **fields: object) -> None:
        """Emit `message` (plus any `**fields`) through the wrapped
        `logging.Logger` at `level`.

        A caller logging a genuinely unhandled exception (not a domain
        `OntolithError`) may pass `exc_info=<the exception>` to get a real
        traceback in the log record, the same as calling
        `logging.Logger.exception()`/`log(..., exc_info=...)` directly
        would — pulled out of `**fields` rather than a dedicated parameter
        so every other sink's `log()` signature stays exactly SPEC §18's
        three-argument shape; a sink that can't act on it just reports it
        as a plain field instead (a non-`BaseException` value is treated
        as a plain field here too, not a malformed `exc_info`).
        """
        exc_info_field = fields.pop("exc_info", None)
        exc_info = exc_info_field if isinstance(exc_info_field, BaseException) else None
        if fields:
            self._logger.log(level, "%s %s", message, fields, exc_info=exc_info)
        else:
            self._logger.log(level, message, exc_info=exc_info)

    def record_event(self, kind: str, **fields: object) -> None:
        """Log `kind` and `**fields` as one INFO-level structured line —
        the tier-(a)/(b) stopgap described in this class's own docstring,
        not a real event store."""
        self._logger.info("event=%s %s", kind, fields)

    def record_metric(self, name: str, value: float, **tags: str) -> None:
        """Log `name`, `value`, and `**tags` as one INFO-level structured
        line — the tier-(a)/(b) stopgap described in this class's own
        docstring, not a real metrics backend."""
        self._logger.info("metric=%s value=%s %s", name, value, tags)


class NullObservabilitySink(ObservabilitySink):
    """No-op sink — for a deployment or test that wants Ontolith to stay
    silent by explicit choice, unlike `StdlibLoggingSink` (the default when
    no sink is injected at all)."""

    def log(self, level: int, message: str, **fields: object) -> None:
        """Discard `level`/`message`/`**fields` — no-op."""

    def record_event(self, kind: str, **fields: object) -> None:
        """Discard `kind`/`**fields` — no-op."""

    def record_metric(self, name: str, value: float, **tags: str) -> None:
        """Discard `name`/`value`/`**tags` — no-op."""


class RecordingObservabilitySink(ObservabilitySink):
    """Test double that accumulates every call (mirrors `FixedClock`/
    `FixedIdProvider`'s role for their own ports) — for tests asserting
    exactly what was logged/recorded, not just that no exception was
    raised."""

    def __init__(self) -> None:
        self.logs: list[tuple[int, str, dict[str, object]]] = []
        self.events: list[tuple[str, dict[str, object]]] = []
        self.metrics: list[tuple[str, float, dict[str, str]]] = []

    def log(self, level: int, message: str, **fields: object) -> None:
        """Append `(level, message, fields)` to `self.logs`."""
        self.logs.append((level, message, fields))

    def record_event(self, kind: str, **fields: object) -> None:
        """Append `(kind, fields)` to `self.events`."""
        self.events.append((kind, fields))

    def record_metric(self, name: str, value: float, **tags: str) -> None:
        """Append `(name, value, tags)` to `self.metrics`."""
        self.metrics.append((name, value, tags))


__all__ = [
    "ObservabilitySink",
    "StdlibLoggingSink",
    "NullObservabilitySink",
    "RecordingObservabilitySink",
]
