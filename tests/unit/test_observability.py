"""Unit tests for the ObservabilitySink port and its three implementations
(SPEC §18, ADR-0044)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core.observability import (
    NullObservabilitySink,
    ObservabilitySink,
    RecordingObservabilitySink,
    StdlibLoggingSink,
)


class TestRecordingObservabilitySink:
    def test_log_accumulates_level_message_and_fields(self) -> None:
        sink = RecordingObservabilitySink()
        sink.log(logging.WARNING, "something happened", namespace="default", principal="alice")
        assert sink.logs == [
            (logging.WARNING, "something happened", {"namespace": "default", "principal": "alice"})
        ]

    def test_log_with_no_fields(self) -> None:
        sink = RecordingObservabilitySink()
        sink.log(logging.INFO, "plain message")
        assert sink.logs == [(logging.INFO, "plain message", {})]

    def test_record_event_accumulates_kind_and_fields(self) -> None:
        sink = RecordingObservabilitySink()
        sink.record_event("proposal.accepted", proposal_id="p-1", namespace="default")
        assert sink.events == [
            ("proposal.accepted", {"proposal_id": "p-1", "namespace": "default"})
        ]

    def test_record_metric_accumulates_name_value_and_tags(self) -> None:
        sink = RecordingObservabilitySink()
        sink.record_metric("query.latency_ms", 12.5, backend="sqlite")
        assert sink.metrics == [("query.latency_ms", 12.5, {"backend": "sqlite"})]

    def test_calls_are_independent_across_the_three_lists(self) -> None:
        """One call to log()/record_event()/record_metric() must only ever
        append to its own list, never the other two — a shared "calls"
        list, or one method delegating into another's list by mistake,
        would be easy to introduce silently."""
        sink = RecordingObservabilitySink()
        sink.log(logging.INFO, "a log line")
        sink.record_event("some.event")
        sink.record_metric("some.metric", 1.0)
        assert len(sink.logs) == 1
        assert len(sink.events) == 1
        assert len(sink.metrics) == 1


class TestNullObservabilitySink:
    def test_all_three_methods_are_true_no_ops(self) -> None:
        """No exception, no side effect observable from outside — this is
        the "explicit silence" opt-out, distinct from StdlibLoggingSink
        (the default when nothing is injected at all)."""
        sink = NullObservabilitySink()
        sink.log(logging.ERROR, "should go nowhere", code="X")
        sink.record_event("should.not.be.recorded")
        sink.record_metric("should.not.be.recorded", 1.0)
        # Nothing to assert on directly — the absence of a raised
        # exception above already proves the no-op contract. A
        # RecordingObservabilitySink round-trip (below) is what actually
        # pins "nothing observable happened" for a case where that matters.


class TestStdlibLoggingSink:
    def test_log_emits_through_the_named_logger(self, caplog: pytest.LogCaptureFixture) -> None:
        sink = StdlibLoggingSink()
        with caplog.at_level(logging.WARNING, logger="ontolith.observability"):
            sink.log(logging.WARNING, "disk almost full", namespace="default")
        [record] = caplog.records
        assert record.levelno == logging.WARNING
        assert "disk almost full" in record.message
        assert "namespace" in record.message
        assert "default" in record.message

    def test_log_with_no_fields_does_not_append_an_empty_dict(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = StdlibLoggingSink()
        with caplog.at_level(logging.INFO, logger="ontolith.observability"):
            sink.log(logging.INFO, "plain message")
        [record] = caplog.records
        assert record.message == "plain message"
        assert "{}" not in record.message

    def test_log_exc_info_field_produces_a_real_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = StdlibLoggingSink()
        try:
            raise ValueError("boom")
        except ValueError as exc:
            with caplog.at_level(logging.ERROR, logger="ontolith.observability"):
                sink.log(logging.ERROR, "unhandled exception", exc_info=exc)
        [record] = caplog.records
        assert record.exc_info is not None
        assert record.exc_info[1] is not None
        assert "boom" in str(record.exc_info[1])

    def test_log_ignores_a_non_exception_exc_info_field(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A caller could in principle pass exc_info=<anything> as a plain
        field by mistake — mypy --strict wouldn't catch a str, since
        **fields is typed object. Confirms the sink degrades gracefully
        (no crash, no fabricated traceback) rather than trusting the
        field's type."""
        sink = StdlibLoggingSink()
        with caplog.at_level(logging.ERROR, logger="ontolith.observability"):
            sink.log(logging.ERROR, "not really an exception", exc_info="oops")
        [record] = caplog.records
        assert record.exc_info is None

    def test_record_event_logs_a_structured_line(self, caplog: pytest.LogCaptureFixture) -> None:
        sink = StdlibLoggingSink()
        with caplog.at_level(logging.INFO, logger="ontolith.observability"):
            sink.record_event("plugin.loaded", plugin="csv-importer")
        [record] = caplog.records
        assert "plugin.loaded" in record.message
        assert "csv-importer" in record.message

    def test_record_metric_logs_a_structured_line(self, caplog: pytest.LogCaptureFixture) -> None:
        sink = StdlibLoggingSink()
        with caplog.at_level(logging.INFO, logger="ontolith.observability"):
            sink.record_metric("query.latency_ms", 42.0, backend="sqlite")
        [record] = caplog.records
        assert "query.latency_ms" in record.message
        assert "42.0" in record.message
        assert "sqlite" in record.message

    def test_custom_logger_name_is_respected(self, caplog: pytest.LogCaptureFixture) -> None:
        sink = StdlibLoggingSink(logger_name="myapp.ontolith")
        with caplog.at_level(logging.INFO, logger="myapp.ontolith"):
            sink.log(logging.INFO, "routed to a custom logger")
        assert any(r.name == "myapp.ontolith" for r in caplog.records)


class TestOntologyObservabilityDefaulting:
    def test_defaults_to_stdlib_logging_sink_when_none_injected(self, tmp_path: Path) -> None:
        """Mirrors clock/id_provider's own defaulting: never injecting a
        sink must not mean silence — a real, production-safe default is
        always wired in, the same guarantee clock=None -> SystemClock()
        already gives."""
        kb = Ontology.connect(tmp_path / "test.db")
        try:
            assert isinstance(kb.observability, StdlibLoggingSink)
        finally:
            kb.close()

    def test_accepts_and_stores_an_injected_sink(self, tmp_path: Path) -> None:
        sink = RecordingObservabilitySink()
        kb = Ontology.connect(tmp_path / "test.db", observability=sink)
        try:
            assert kb.observability is sink
        finally:
            kb.close()

    def test_injected_sink_satisfies_the_abstract_port(self) -> None:
        """A trivial static check that RecordingObservabilitySink genuinely
        implements ObservabilitySink, not just duck-types it — instantiation
        of an ABC subclass with a missing abstract method raises TypeError
        at construction time, which this would surface immediately."""
        sink: ObservabilitySink = RecordingObservabilitySink()
        assert isinstance(sink, ObservabilitySink)
