"""Unit tests for the plugin process-isolation sandbox (ADR-0051).

Entry-point discovery inside a sandboxed child process is NOT
monkeypatchable the way tests/unit/test_plugin_registry.py's test doubles
are: multiprocessing's "spawn" start method launches a genuinely fresh
Python interpreter that re-imports everything from scratch, so a
monkeypatch applied in the parent test process never reaches it. Tests here
that exercise a real spawned child therefore use the real, installed
reference plugins (csv-importer, json-exporter, rdf-owl-exporter,
required-fields-validator) via their real "ontolith.plugins" entry points —
run_isolated() called directly, not always through PluginRegistry, for
tests that need a specific error condition a reference plugin's own code
already raises (e.g. CsvImporter's ValidationError on a malformed row)
rather than a full registration.

Pure marshalling logic (wire.py) and anything that raises before touching
a connection (RemoteReadOnlyView.query()/.as_of()) are tested directly,
with no subprocess involved at all.

Real, OS-level seccomp enforcement (enforcement.py) is only verifiable on
Linux with libseccomp installed - this project's own darwin/macOS
development environment cannot exercise it at all, only CI's ubuntu-latest
job can (see .github/workflows/ci.yml's libseccomp2 install step). Those
tests are skipped everywhere else, run in their own dedicated subprocess
even on Linux (a real seccomp filter can only get MORE restrictive once
loaded into a process, never removed - installing one directly in the
pytest worker process would permanently sandbox it for the rest of the
test session).
"""

from __future__ import annotations

import io
import logging
import multiprocessing
import sys
import tempfile
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core.errors import PluginError, ValidationError
from ontolith.core.observability import NullObservabilitySink, RecordingObservabilitySink
from ontolith.plugins.manifest import PluginCapabilities
from ontolith.plugins.registry import PluginRegistry
from ontolith.plugins.sandbox import enforcement, protocol, runner
from ontolith.plugins.sandbox.remote_view import RemoteReadOnlyView, RemoteWritable, RemoteWriteView
from ontolith.plugins.sandbox.runner import IsolatedPluginProxy, run_isolated
from ontolith.plugins.sandbox.wire import (
    RemoteViewMarker,
    RemoteWritableMarker,
    UnwirableArgumentError,
    restricted_loads,
    unwire_exception,
    wire_exception,
    wire_value,
)
from ontolith.plugins.views import ReadOnlyView, WriteView

ADMIN = "admin@example.com"


class _ModulePicklableValueError(ValueError):
    """Module-level (so it's actually picklable - pickle needs an
    importable qualname) stand-in for a plugin-defined exception type
    that subclasses a builtin RestrictedUnpickler allows, without being
    on the allowlist itself."""


@pytest.fixture
def kb() -> Ontology:
    with tempfile.TemporaryDirectory() as tmpdir:
        kb = Ontology.connect(Path(tmpdir) / "test.db")
        kb.create_principal(ADMIN, kind="human", default_capability="admin")
        yield kb
        kb.close()


class TestWireValue:
    """wire.wire_value's structural detection, no subprocess needed."""

    def test_a_plain_picklable_value_passes_through_unchanged(self) -> None:
        value = {"a": [1, 2, 3]}
        assert wire_value(value, "tag") is value

    def test_a_readonly_view_becomes_a_non_writable_marker(self, kb: Ontology) -> None:
        view = ReadOnlyView(kb, ADMIN)
        wired = wire_value(view, "kb")
        assert wired == RemoteViewMarker(tag="kb", writable=False, principal_id=ADMIN)

    def test_a_write_view_becomes_a_writable_marker(self, kb: Ontology) -> None:
        view = WriteView(kb, ADMIN)
        wired = wire_value(view, "kb")
        assert wired == RemoteViewMarker(tag="kb", writable=True, principal_id=ADMIN)

    def test_an_unpicklable_write_shaped_value_becomes_a_writable_marker(self) -> None:
        buf = io.StringIO()
        wired = wire_value(buf, "arg_1")
        assert wired == RemoteWritableMarker(tag="arg_1")

    def test_a_picklable_write_shaped_value_is_still_proxied_not_copied(self) -> None:
        """io.StringIO pickles successfully (into a disconnected copy) —
        must still be proxied, or a plugin's write would land on a copy
        the caller never sees (the exact bug this test pins, caught while
        building this feature — see wire.py's own comment)."""
        buf = io.StringIO()
        import pickle

        pickle.dumps(buf)  # sanity: confirms the tricky case is real
        wired = wire_value(buf, "arg_1")
        assert isinstance(wired, RemoteWritableMarker)

    def test_an_unpicklable_non_writable_value_raises(self) -> None:
        unpicklable = (x for x in range(3))  # a generator - no .write, not picklable
        with pytest.raises(UnwirableArgumentError, match="cannot cross the plugin sandbox"):
            wire_value(unpicklable, "arg_0")


class TestWireException:
    """wire.wire_exception/unwire_exception, no subprocess needed."""

    def test_an_ontolith_error_round_trips_with_detail_intact(self) -> None:
        original = ValidationError("bad row", detail={"row": 3, "field": "value_kind"})
        wired = wire_exception(original)
        restored = unwire_exception(wired)
        assert isinstance(restored, ValidationError)
        assert restored.message == "bad row"
        assert restored.detail == {"row": 3, "field": "value_kind"}
        assert restored.code == "VALIDATION_ERROR"

    def test_plain_pickling_already_preserves_detail(self) -> None:
        """BaseException's own __reduce__ includes __dict__ as
        reconstruction state, not just self.args - confirms wire_exception
        doesn't need OntolithError-specific handling, only a fallback for
        a third-party exception that isn't picklable at all."""
        import pickle

        original = ValidationError("bad row", detail={"row": 3})
        naive = pickle.loads(pickle.dumps(original))
        assert naive.detail == {"row": 3}
        assert naive.message == "bad row"

    def test_a_plain_picklable_exception_round_trips_as_itself(self) -> None:
        original = ValueError("plain")
        wired = wire_exception(original)
        restored = unwire_exception(wired)
        assert isinstance(restored, ValueError)
        assert str(restored) == "plain"

    def test_an_unpicklable_third_party_exception_degrades_to_runtimeerror(self) -> None:
        class _Unpicklable(Exception):
            def __init__(self) -> None:
                super().__init__("boom")
                self.unpicklable_attr = lambda: None  # closures aren't picklable

        original = _Unpicklable()
        wired = wire_exception(original)
        restored = unwire_exception(wired)
        assert isinstance(restored, RuntimeError)
        assert "_Unpicklable" in str(restored)
        assert "boom" in str(restored)

    def test_a_picklable_third_party_exception_also_degrades_to_runtimeerror(self) -> None:
        """Round-2 review finding: a plugin-defined exception subclassing
        an allowlisted builtin (e.g. ValueError) is picklable, but its
        concrete class isn't on RestrictedUnpickler's allowlist - an
        earlier version of this check used the MRO instead of the
        concrete class, which always matched (every exception's MRO
        includes "builtins.Exception", itself allowlisted), so this case
        was never degraded and the message was destroyed wholesale when
        the parent's restricted unpickler refused the concrete class."""

        # _ModulePicklableValueError, not a locally-defined class here - a
        # class defined inside a test function/method isn't picklable at
        # all (pickle needs a module-level, importable qualname), which
        # would make the "picklable" premise of this test false.
        original = _ModulePicklableValueError("custom message")
        import pickle

        pickle.dumps(original)  # sanity: confirms this case really is picklable

        wired = wire_exception(original)
        assert isinstance(wired, RuntimeError)
        assert "_ModulePicklableValueError" in str(wired)
        assert "custom message" in str(wired)

        # And restricted_loads must actually accept the degraded form -
        # this is what a real cross-process round trip does, not just
        # wire_exception/unwire_exception in the same process.
        restored = restricted_loads(pickle.dumps(wired))
        assert isinstance(restored, RuntimeError)
        assert "custom message" in str(restored)


class TestRemoteWritable:
    """RemoteWritable's own request/reply plumbing, no subprocess needed —
    a fake connection stands in for the pipe end."""

    def test_write_sends_one_call_message_and_returns_the_reply(self) -> None:
        sent: list[object] = []

        class _FakeConn:
            def send(self, message: object) -> None:
                sent.append(message)

            def recv(self) -> tuple[str, object]:
                from ontolith.plugins.sandbox import protocol

                return (protocol.RESULT, 3)

        writable = RemoteWritable(conn=_FakeConn(), tag="buf")
        result = writable.write("abc")

        assert result == 3
        [(kind, tag, method_name, args, kwargs)] = sent
        assert (kind, tag, method_name, args, kwargs) == ("call", "buf", "write", ("abc",), {})

    def test_write_raises_the_unwired_exception_on_an_error_reply(self) -> None:
        class _FakeConn:
            def send(self, message: object) -> None:
                pass

            def recv(self) -> tuple[str, object]:
                from ontolith.plugins.sandbox import protocol

                return (protocol.ERROR, ValueError("closed"))

        writable = RemoteWritable(conn=_FakeConn(), tag="buf")
        with pytest.raises(ValueError, match="closed"):
            writable.write("abc")


class TestRemoteViewQueryAsOfUnsupported:
    """These raise before ever touching a connection - no subprocess needed."""

    def test_query_raises_plugin_error(self) -> None:
        view = RemoteReadOnlyView(conn=None, tag="kb", principal_id="plugin@example.com")
        with pytest.raises(PluginError, match="not available from inside a sandboxed"):
            view.query("Person")

    def test_as_of_raises_plugin_error(self) -> None:
        view = RemoteReadOnlyView(conn=None, tag="kb", principal_id="plugin@example.com")
        with pytest.raises(PluginError, match="not available from inside a sandboxed"):
            view.as_of("2026-01-01")

    def test_write_view_inherits_the_same_restriction(self) -> None:
        view = RemoteWriteView(conn=None, tag="kb", principal_id="plugin@example.com")
        with pytest.raises(PluginError):
            view.query("Person")


class TestEnforcementAvailability:
    """Platform-conditional, no subprocess needed for the negative case."""

    def test_no_enforcement_available_on_a_non_linux_platform(self) -> None:
        if sys.platform == "linux":
            pytest.skip("this assertion is specifically about non-Linux platforms")
        assert enforcement.enforcement_available() is False

    def test_apply_capability_enforcement_degrades_gracefully_off_linux(self) -> None:
        if sys.platform == "linux":
            pytest.skip("this assertion is specifically about non-Linux platforms")
        result = enforcement.apply_capability_enforcement(
            PluginCapabilities(network=False, filesystem=False)
        )
        assert result.applied is False
        assert result.reason is not None
        assert "linux" in result.reason.lower() or sys.platform in result.reason


def _linux_seccomp_available() -> bool:
    if sys.platform != "linux":
        return False
    try:
        import pyseccomp  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.skipif(
    not _linux_seccomp_available(), reason="real seccomp enforcement needs Linux + libseccomp"
)
class TestRealSeccompEnforcementOnLinux:
    """Runs each check in its own dedicated subprocess - a loaded seccomp
    filter only ever gets more restrictive, never removed, so installing
    one directly in the pytest worker process would permanently sandbox it
    for the rest of the test session."""

    @staticmethod
    def _probe_network_denied(result_queue: multiprocessing.Queue[bool]) -> None:
        import socket

        from ontolith.plugins.manifest import PluginCapabilities
        from ontolith.plugins.sandbox import enforcement

        enforcement.apply_capability_enforcement(PluginCapabilities(network=False))
        try:
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result_queue.put(False)  # not denied - filter didn't take effect
        except PermissionError:
            result_queue.put(True)

    @staticmethod
    def _probe_network_allowed(result_queue: multiprocessing.Queue[bool]) -> None:
        import socket

        from ontolith.plugins.manifest import PluginCapabilities
        from ontolith.plugins.sandbox import enforcement

        enforcement.apply_capability_enforcement(PluginCapabilities(network=True))
        try:
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
            result_queue.put(True)
        except PermissionError:
            result_queue.put(False)

    def _run_probe(self, target: object) -> bool:
        ctx = multiprocessing.get_context("spawn")
        queue: multiprocessing.Queue[bool] = ctx.Queue()
        process = ctx.Process(target=target, args=(queue,))
        process.start()
        outcome = queue.get(timeout=10)
        process.join(timeout=5)
        return outcome

    def test_network_false_denies_socket_creation(self) -> None:
        assert self._run_probe(self._probe_network_denied) is True

    def test_network_true_allows_socket_creation(self) -> None:
        assert self._run_probe(self._probe_network_allowed) is True


class TestRunIsolatedAgainstRealReferencePlugins:
    """Full round trip through a real spawned child, using the actually-
    installed reference plugins' real "ontolith.plugins" entry points
    (test-double plugins under tests/fixtures/plugins/ are NOT resolvable
    from inside a spawned child - see this module's own docstring)."""

    def test_validation_error_detail_survives_the_process_boundary(self, kb: Ontology) -> None:
        """CsvImporter._require_columns raises ValidationError with a real
        detail dict for a malformed row - confirms the exact exception
        type and its detail both survive a real subprocess round trip."""
        loaded = PluginRegistry(kb).register(
            "csv-importer", author=ADMIN, granted_capability="write"
        )
        bad_rows = [{"concept": "Person"}]  # missing every other required column

        with pytest.raises(ValidationError) as exc_info:
            loaded.instance.import_(bad_rows, loaded.view)

        assert exc_info.value.code == "VALIDATION_ERROR"
        assert exc_info.value.detail.get("row") == 1
        assert "missing_columns" in exc_info.value.detail

    def test_unwirable_argument_is_rejected_before_any_process_is_spawned(
        self, kb: Ontology
    ) -> None:
        view = (
            PluginRegistry(kb)
            .register("csv-importer", author=ADMIN, granted_capability="write")
            .view
        )
        unpicklable_source = (row for row in [])  # a generator - no .write, not picklable

        with pytest.raises(UnwirableArgumentError):
            run_isolated(
                "csv-importer",
                "import_",
                PluginCapabilities(storage="write"),
                (unpicklable_source, view),
                {},
                kb.observability,
            )

    def test_unknown_entry_point_raises_plugin_error(self, kb: Ontology) -> None:
        view = ReadOnlyView(kb, ADMIN)
        with pytest.raises(PluginError, match="entry point not found"):
            run_isolated(
                "definitely-not-a-real-plugin",
                "export",
                PluginCapabilities(),
                (view, io.StringIO()),
                {},
                kb.observability,
            )

    def test_isolated_proxy_exposes_the_underlying_plugin_class(self, kb: Ontology) -> None:
        loaded = PluginRegistry(kb).register("json-exporter", author=ADMIN)
        assert isinstance(loaded.instance, IsolatedPluginProxy)
        from ontolith.plugins.reference.json_exporter import JsonExporter

        assert loaded.instance.plugin_class is JsonExporter

    def test_validator_end_to_end_through_a_registry_loaded_view(self, kb: Ontology) -> None:
        """H1 (round-1 finding): a PluginRegistry-loaded validator's kb is
        a real ReadOnlyView (not the trusted Ontology instance passed when
        a validator is wired into Ontology's own validators/
        completeness_validators parameters instead - see ports.py's
        ValidatorKbView docstring, that combination is unsupported and
        unrelated to PluginRegistry). Calling it the same way every other
        plugin kind is called through the registry works correctly -
        round-2 review finding: this exact path had no test before."""
        from ontolith.schema import ConceptDef, PropertyDef, SchemaIR

        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person", properties={"name": PropertyDef(name="name", value_type="Text")}
                )
            },
        )
        kb.apply_schema(schema, author=ADMIN)
        entity = kb.create_entity("Person", author=ADMIN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada Lovelace", "Text", ADMIN)

        loaded = PluginRegistry(kb).register("required-fields-validator", author=ADMIN)
        assert isinstance(loaded.instance, IsolatedPluginProxy)

        violations = loaded.instance.validate(assertion, loaded.view)
        assert violations == []

        # And a genuine violation is reported correctly too, not just the
        # empty-list happy path.
        incomplete_entity = kb.create_entity("Person", author=ADMIN)
        incomplete_assertion = kb.assert_literal(
            incomplete_entity.id, "Person.name", "", "Text", ADMIN
        )
        kb.retract(incomplete_assertion.id, ADMIN)
        violations = loaded.instance.validate(incomplete_assertion, loaded.view)
        assert violations
        assert "missing required predicate" in violations[0]

    def test_json_export_end_to_end_via_a_real_writable_proxy(self, kb: Ontology) -> None:
        from ontolith.schema import ConceptDef, PropertyDef, SchemaIR

        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person", properties={"name": PropertyDef(name="name", value_type="Text")}
                )
            },
        )
        kb.apply_schema(schema, author=ADMIN)
        entity = kb.create_entity("Person", author=ADMIN)
        kb.assert_literal(entity.id, "Person.name", "Ada Lovelace", "Text", ADMIN)

        loaded = PluginRegistry(kb).register("json-exporter", author=ADMIN)
        buf = io.StringIO()
        report = loaded.instance.export(loaded.view, buf)

        # A dataclass return value crosses an isolated call as a plain dict
        # (wire.to_wire_result, security review finding) - isolate=False
        # would return the real ExportReport instance unchanged.
        assert report == {"assertions_written": 1}
        assert "Ada Lovelace" in buf.getvalue()


# --- Module-level child-process targets --------------------------------
# multiprocessing's "spawn" pickles the target by reference (module +
# qualname), so these can't be closures/lambdas defined inside a test.


def _child_sends_hostile_reduce(child_conn: object) -> None:
    """CRITICAL-1 PoC: a plugin returning an object whose __reduce__ names
    an arbitrary callable, as the DONE message's own result."""
    import os
    import pickle

    class _Hostile:
        def __reduce__(self) -> tuple[object, tuple[str]]:
            return (os.system, (f"touch {_MARKER_PATH}",))  # nosec B605 - test-only PoC

    child_conn.send_bytes(pickle.dumps((protocol.DONE, _Hostile())))  # type: ignore[attr-defined]


def _child_attempts_setattr_escalation(child_conn: object) -> None:
    """CRITICAL-2 PoC: a plugin calling __setattr__ on its own kb proxy's
    tag, targeting the parent's real view object directly - no pickle
    trickery, plain strings."""
    import pickle

    child_conn.send_bytes(  # type: ignore[attr-defined]
        pickle.dumps(
            (protocol.CALL, "kb", "__setattr__", ("_principal_id", "admin@example.com"), {})
        )
    )
    child_conn.recv()  # type: ignore[attr-defined]  # the ERROR reply, discarded
    child_conn.send_bytes(pickle.dumps((protocol.DONE, "done")))  # type: ignore[attr-defined]


def _child_calls_write_capable_method_on_a_readonly_tag(child_conn: object) -> None:
    """_allowed_methods_for must be keyed to the real object's actual
    type, not a flat "any allowed name anywhere" set - a plugin with only
    ReadOnlyView access must not reach create_entity/propose/propose_ref/
    retract, even though those names are valid on a WriteView tag."""
    import pickle

    child_conn.send_bytes(  # type: ignore[attr-defined]
        pickle.dumps((protocol.CALL, "kb", "create_entity", ("Person",), {}))
    )
    kind, payload = child_conn.recv()  # type: ignore[attr-defined]
    child_conn.send_bytes(pickle.dumps((protocol.DONE, (kind, str(payload)))))  # type: ignore[attr-defined]


_MARKER_PATH = "/tmp/ontolith_test_plugin_sandbox_pwned_marker"  # nosec B108 - test-only


class TestDispatchLoopSecurityBoundary:
    """Direct reproduction of both CRITICAL findings against the real
    _dispatch_loop, using a real multiprocessing.Pipe and a real spawned
    child - the same level this project's own review rounds verified at,
    not a diff-reading confirmation (round-2 review finding: the fix
    commit shipped this exact boundary with no regression test at all)."""

    def test_hostile_reduce_payload_does_not_execute_in_the_parent(self, tmp_path: Path) -> None:
        import os

        if os.path.exists(_MARKER_PATH):
            os.remove(_MARKER_PATH)

        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        process = ctx.Process(target=_child_sends_hostile_reduce, args=(child_conn,))
        process.start()
        child_conn.close()
        try:
            with pytest.raises(PluginError, match="malformed or disallowed"):
                runner._dispatch_loop(parent_conn, process, {}, "import_", NullObservabilitySink())
        finally:
            parent_conn.close()
            process.join(timeout=5)

        assert not os.path.exists(_MARKER_PATH)
        if os.path.exists(_MARKER_PATH):  # pragma: no cover - cleanup only if the PoC leaked
            os.remove(_MARKER_PATH)

    def test_setattr_escalation_does_not_reach_the_real_view(self, kb: Ontology) -> None:
        kb.create_principal("lowtrust-plugin", kind="service", default_capability="write")
        view = WriteView(kb, "lowtrust-plugin")

        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        process = ctx.Process(target=_child_attempts_setattr_escalation, args=(child_conn,))
        process.start()
        child_conn.close()
        try:
            result = runner._dispatch_loop(
                parent_conn, process, {"kb": view}, "import_", NullObservabilitySink()
            )
            assert result == "done"
        finally:
            parent_conn.close()
            process.join(timeout=5)

        assert view.principal_id == "lowtrust-plugin"

    def test_readonly_tag_cannot_reach_write_capable_methods(self, kb: Ontology) -> None:
        """The allow-list must be keyed to the real object's type via
        isinstance, not a flat set of "any allowed name anywhere" -
        create_entity is a real, allowed WriteView method, but must still
        be refused against a tag that maps to a plain ReadOnlyView."""
        view = ReadOnlyView(kb, ADMIN)

        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        process = ctx.Process(
            target=_child_calls_write_capable_method_on_a_readonly_tag, args=(child_conn,)
        )
        process.start()
        child_conn.close()
        try:
            kind, message = runner._dispatch_loop(
                parent_conn, process, {"kb": view}, "import_", NullObservabilitySink()
            )
        finally:
            parent_conn.close()
            process.join(timeout=5)

        assert kind == protocol.ERROR
        assert "not permitted" in message


class TestEnforcementWarningReachesObservability:
    """H3 (round-1 finding): a call-time enforcement failure must be
    reported to the caller's observability sink, not silently discarded -
    the registration-time warning can only predict this, not guarantee
    it."""

    def test_enforcement_not_applied_logs_a_warning(self, kb: Ontology) -> None:
        # IsolatedPluginProxy captures kb.observability at registration
        # time (registry.py) - the sink must be swapped in before
        # register() runs, not after.
        sink = RecordingObservabilitySink()
        kb.observability = sink
        loaded = PluginRegistry(kb).register("json-exporter", author=ADMIN, isolate=True)

        buf = io.StringIO()
        loaded.instance.export(loaded.view, buf)

        enforcement_logs = [
            (level, message, fields)
            for level, message, fields in sink.logs
            if "capability enforcement did not apply" in message
        ]
        if sys.platform != "linux" or not _linux_seccomp_available():
            assert enforcement_logs, "expected a call-time enforcement warning on this platform"
            level, message, fields = enforcement_logs[0]
            assert level == logging.WARNING
            assert fields["method"] == "export"
