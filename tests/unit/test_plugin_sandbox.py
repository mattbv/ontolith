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
import time
from pathlib import Path
from typing import Any

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


class TestProcessIsolationFloorContents:
    """Pins the always-on floor's exact syscall set - platform-independent
    (a plain tuple comparison, no filter installed), so it runs everywhere,
    unlike TestRealSeccompEnforcementOnLinux below. Security review found
    this floor had no test at all pinning its contents: removing any of the
    round-2 pidfd/kcmp/process_madvise/process_mrelease additions passed
    the whole suite silently on a non-Linux dev machine."""

    def test_floor_denies_the_full_cross_process_introspection_family(self) -> None:
        assert set(enforcement._PROCESS_ISOLATION_SYSCALLS) == {
            "ptrace",
            "process_vm_readv",
            "process_vm_writev",
            "pidfd_open",
            "pidfd_getfd",
            "pidfd_send_signal",
            "kcmp",
            "process_madvise",
            "process_mrelease",
        }


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


def _child_forges_enforcement_true_then_genuine_false(child_conn: object) -> None:
    """A malicious child sends applied=True first (to try to consume the
    "already reported" slot), then the genuine applied=False - the real
    report must still reach the sink."""
    import pickle

    child_conn.send_bytes(pickle.dumps((protocol.ENFORCEMENT, True, None)))  # type: ignore[attr-defined]
    child_conn.send_bytes(  # type: ignore[attr-defined]
        pickle.dumps((protocol.ENFORCEMENT, False, "genuine failure"))
    )
    child_conn.send_bytes(pickle.dumps((protocol.DONE, "ok")))  # type: ignore[attr-defined]


def _child_floods_genuine_enforcement_false(child_conn: object) -> None:
    import pickle

    for _ in range(50):
        child_conn.send_bytes(pickle.dumps((protocol.ENFORCEMENT, False, "flood")))  # type: ignore[attr-defined]
    child_conn.send_bytes(pickle.dumps((protocol.DONE, "ok")))  # type: ignore[attr-defined]


def _child_sends_genuine_enforcement_false_then_done(child_conn: object) -> None:
    """A well-behaved (non-adversarial) child whose real enforcement
    attempt genuinely failed - used to test require_enforcement's
    call-time refusal directly against `_dispatch_loop`, without needing
    a real platform/host where enforcement actually fails."""
    import pickle

    child_conn.send_bytes(  # type: ignore[attr-defined]
        pickle.dumps((protocol.ENFORCEMENT, False, "simulated failure"))
    )
    child_conn.send_bytes(pickle.dumps((protocol.DONE, "ok")))  # type: ignore[attr-defined]


def _child_sends_empty_message_then_sleeps(child_conn: object) -> None:
    """restricted_loads(b"") raises EOFError - indistinguishable from a
    closed pipe at that layer - but a child that sends empty bytes and
    then keeps running is still alive. The old EOF handler did a bare,
    unbounded process.join(), which would hang here forever."""
    import time

    child_conn.send_bytes(b"")  # type: ignore[attr-defined]
    time.sleep(30)  # still alive, never sends anything else


def _child_sends_corrupt_pickle_bytes(child_conn: object) -> None:
    """Not pickle.UnpicklingError specifically - a corrupt protocol byte
    raises a plain ValueError from inside pickle's own decoder, uncaught
    by a narrower except clause."""
    child_conn.send_bytes(b"\x80\x63.")  # type: ignore[attr-defined]  # bogus protocol byte


def _child_calls_then_closes_its_pipe(child_conn: object) -> None:
    """Round-4 review finding: a fully non-adversarial scenario - the
    plugin makes an ordinary view call, then its process ends (crash,
    OOM-kill, os._exit) before it ever reads the reply. Closing our own
    end explicitly (not just exiting) forces the parent's reply send to
    fail with a genuine BrokenPipeError, not an EOFError race on a
    subsequent recv."""
    import pickle
    import time

    child_conn.send_bytes(  # type: ignore[attr-defined]
        pickle.dumps((protocol.CALL, "kb", "get_entity", ("x",), {}))
    )
    child_conn.close()  # type: ignore[attr-defined]
    time.sleep(5)  # stay alive so this isn't an EOF-on-exit race either


def _child_sends_unknown_protocol_kind(child_conn: object) -> None:
    import pickle
    import time

    child_conn.send_bytes(pickle.dumps(("bogus-kind", 1)))  # type: ignore[attr-defined]
    time.sleep(30)


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


class TestRequireEnforcement:
    """Security review finding, M4 Workstream 7: the registration-time
    warning (TestEnforcementWarningReachesObservability above) is the only
    signal an operator gets when OS-level enforcement doesn't apply - it's
    advisory, not a guarantee. `require_enforcement` gives a real lever:
    refuse at registration when enforcement can't even be attempted
    (isolate=False, or unavailable on this host/platform), and refuse each
    isolated call when a specific attempt to install it fails, rather than
    silently proceeding with capabilities.network/.filesystem unenforced.
    """

    def test_dispatch_loop_raises_when_call_time_enforcement_genuinely_fails(
        self, kb: Ontology
    ) -> None:
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        process = ctx.Process(
            target=_child_sends_genuine_enforcement_false_then_done, args=(child_conn,)
        )
        process.start()
        child_conn.close()
        try:
            with pytest.raises(PluginError, match="require_enforcement"):
                runner._dispatch_loop(
                    parent_conn,
                    process,
                    {},
                    "import_",
                    NullObservabilitySink(),
                    require_enforcement=True,
                )
        finally:
            parent_conn.close()
            process.join(timeout=5)

    def test_dispatch_loop_still_only_warns_when_require_enforcement_is_false(
        self, kb: Ontology
    ) -> None:
        """The default (require_enforcement=False) preserves today's
        behavior exactly - this is the same scenario as the raising test
        above, differing only in this one flag."""
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        process = ctx.Process(
            target=_child_sends_genuine_enforcement_false_then_done, args=(child_conn,)
        )
        process.start()
        child_conn.close()
        sink = RecordingObservabilitySink()
        try:
            result = runner._dispatch_loop(
                parent_conn, process, {}, "import_", sink, require_enforcement=False
            )
        finally:
            parent_conn.close()
            process.join(timeout=5)

        assert result == "ok"
        assert len(sink.logs) == 1

    def test_register_with_require_enforcement_raises_when_isolate_false(
        self, kb: Ontology
    ) -> None:
        with pytest.raises(PluginError, match="require_enforcement"):
            PluginRegistry(kb).register(
                "json-exporter", author=ADMIN, isolate=False, require_enforcement=True
            )

    def test_register_with_require_enforcement_raises_when_enforcement_unavailable(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(enforcement, "enforcement_available", lambda: False)
        with pytest.raises(PluginError, match="require_enforcement"):
            PluginRegistry(kb).register(
                "json-exporter", author=ADMIN, isolate=True, require_enforcement=True
            )

    def test_register_with_require_enforcement_succeeds_when_enforcement_available(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Registration-time check only - whether this specific call later
        succeeds under the real filter is the dispatch-loop tests' own
        concern, exercised for real on Linux+libseccomp by
        TestEnforcementWarningReachesObservability and
        TestRealSeccompEnforcementOnLinux above."""
        monkeypatch.setattr(enforcement, "enforcement_available", lambda: True)
        loaded = PluginRegistry(kb).register(
            "json-exporter", author=ADMIN, isolate=True, require_enforcement=True
        )
        assert isinstance(loaded.instance, IsolatedPluginProxy)

    def test_run_isolated_raises_and_leaves_the_remote_target_unwritten(self, kb: Ontology) -> None:
        """End-to-end: run_isolated raises PluginError naming
        require_enforcement, and `buf` (a RemoteWritable-proxied target)
        never receives any partial/garbage write. NOT a proof that
        export() itself never ran - it can't be, for this class of target:
        `buf.write(...)` inside the child sends a CALL message and blocks
        for the parent's reply, and the parent's dispatch loop stops
        reading messages the moment it raises (on the very first
        ENFORCEMENT(applied=False) message, whether or not the child-side
        require_enforcement check exists) - so a proxied write can never
        land in `buf` once the parent has decided to raise, regardless of
        whether the child went on to call export() or not. Verified by
        mutation: removing the child-side check (round 1's own bug) still
        leaves this exact assertion passing. See
        `test_child_main_checks_require_enforcement_before_calling_the_
        plugins_protocol_method` below for the actual proof that the
        protocol method call is structurally unreachable when required
        enforcement doesn't apply."""
        if enforcement.enforcement_available():
            pytest.skip("this needs a platform where enforcement is NOT available")
        loaded = PluginRegistry(kb).register("json-exporter", author=ADMIN, isolate=True)
        buf = io.StringIO()
        with pytest.raises(PluginError, match="require_enforcement"):
            run_isolated(
                "json-exporter",
                "export",
                loaded.manifest.capabilities,
                (loaded.view, buf),
                {},
                NullObservabilitySink(),
                require_enforcement=True,
            )
        assert buf.getvalue() == ""

    def test_child_main_checks_require_enforcement_before_calling_the_plugins_protocol_method(
        self,
    ) -> None:
        """Structural proof the black-box test above can't provide (see its
        own docstring for why): _child_main's own source must check
        require_enforcement strictly BEFORE the line that calls the
        plugin's protocol method (`getattr(plugin_instance, method_name)
        (...)`), not merely raise a PluginError somewhere in the function.
        Round-1 review's own bug shipped exactly this shape backwards: the
        equivalent check lived only in the PARENT's message handler, with
        nothing in `_child_main` itself gating the method call at all -
        this test pins the child's own control flow directly, via AST
        inspection, the same verification method used to confirm the
        HIGH-1 reordering fix during this review."""
        import ast
        import inspect

        source = inspect.getsource(runner._child_main)
        tree = ast.parse(source)
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)

        require_enforcement_check_line: int | None = None
        protocol_method_call_line: int | None = None
        for node in ast.walk(func):
            if (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.BoolOp)
                and any(
                    isinstance(v, ast.Name) and v.id == "require_enforcement"
                    for v in node.test.values
                )
            ):
                require_enforcement_check_line = node.lineno
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Call)
                and isinstance(node.func.func, ast.Name)
                and node.func.func.id == "getattr"
            ):
                protocol_method_call_line = node.lineno

        assert require_enforcement_check_line is not None, (
            "no 'if require_enforcement ...' check found in _child_main"
        )
        assert protocol_method_call_line is not None, (
            "no getattr(plugin_instance, method_name)(...) call found in _child_main"
        )
        assert require_enforcement_check_line < protocol_method_call_line, (
            "require_enforcement is checked AFTER the plugin's protocol method is already "
            "called - the check must come first for it to mean anything"
        )

    def test_child_main_never_invokes_the_protocol_method_when_required_enforcement_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Behavioral proof the AST test above can't provide (round-3
        review finding): comparing line numbers only checks TEXTUAL order,
        not actual control flow - reproduced directly: removing just the
        `return` from _child_main's own require_enforcement branch (so the
        function falls through to call the plugin anyway after sending
        FAILED) left the AST-ordering test above passing unchanged, since
        the `if` and the `getattr(...)` call are still in the same
        relative source order. This test instead runs _child_main for
        real, with every dependency it touches monkeypatched to a
        controlled double, and asserts the plugin's own method was never
        actually invoked - a `return`-less branch would fail this
        immediately (mutation-tested: confirmed failing against the exact
        no-return mutant described above before this test was added)."""
        method_calls: list[str] = []

        class _FakePlugin:
            def export(self, *args: object, **kwargs: object) -> str:
                method_calls.append("export")
                return "ran"

        class _FakeConn:
            def __init__(self) -> None:
                self.sent: list[tuple[Any, ...]] = []

            def send(self, message: tuple[Any, ...]) -> None:
                self.sent.append(message)

        monkeypatch.setattr(runner, "_load_plugin_instance", lambda name: _FakePlugin())
        monkeypatch.setattr(
            enforcement,
            "apply_preimport_enforcement",
            lambda caps: enforcement.EnforcementResult(applied=True, reason=None),
        )
        monkeypatch.setattr(
            enforcement,
            "apply_capability_enforcement",
            lambda caps: enforcement.EnforcementResult(applied=False, reason="simulated"),
        )

        conn = _FakeConn()
        runner._child_main(
            conn,
            "irrelevant-entry-point",
            "export",
            PluginCapabilities(),
            (),
            {},
            require_enforcement=True,
        )

        assert method_calls == [], "the plugin's own protocol method must never run"
        kinds = [message[0] for message in conn.sent]
        assert protocol.FAILED in kinds, f"expected a FAILED message, got {kinds}"
        assert protocol.DONE not in kinds, f"a DONE message means the plugin actually ran: {kinds}"

    def test_capturing_the_enforcement_function_before_load_defeats_the_attribute_attack(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-3 review finding, reproduced end to end in this session
        against a real installed entry point before this fix, and again
        after (confirming the fix): a plugin's own module-level code, which
        necessarily runs before the full filter is ever attempted (see
        `apply_preimport_enforcement`'s own docstring for why), could
        reassign `enforcement.apply_capability_enforcement` itself to fake
        a successful install - completely defeating `require_enforcement=
        True` with no exception and no warning. `_child_main` now captures
        the real function as a local variable BEFORE calling `_load_
        plugin_instance`, closing the specific module-attribute version of
        this attack: reassigning the module's own attribute during load no
        longer matters once `_child_main` already holds a reference to the
        original function object. This test simulates the attack directly
        (the malicious reassignment happens inside the monkeypatched `_load
        _plugin_instance`, standing in for a plugin's own module import)
        and asserts the captured reference still reports the true,
        unenforced (`applied=False` on this non-Linux test machine) result,
        not the plugin's forged `applied=True` - i.e. `require_enforcement`
        still fires correctly despite the plugin's own attempt to suppress
        it."""
        real_apply_capability_enforcement = enforcement.apply_capability_enforcement

        def _malicious_load(name: str) -> object:
            # Simulates a plugin's own module-level code reassigning the
            # module attribute during its own import/construction.
            enforcement.apply_capability_enforcement = lambda caps: enforcement.EnforcementResult(
                applied=True, reason=None
            )

            class _EvilPlugin:
                def export(self, *args: object, **kwargs: object) -> str:
                    return "ran despite filesystem=False"

            return _EvilPlugin()

        class _FakeConn:
            def __init__(self) -> None:
                self.sent: list[tuple[Any, ...]] = []

            def send(self, message: tuple[Any, ...]) -> None:
                self.sent.append(message)

        monkeypatch.setattr(runner, "_load_plugin_instance", _malicious_load)
        monkeypatch.setattr(
            enforcement,
            "apply_preimport_enforcement",
            lambda caps: enforcement.EnforcementResult(applied=True, reason=None),
        )
        try:
            conn = _FakeConn()
            runner._child_main(
                conn,
                "irrelevant-entry-point",
                "export",
                PluginCapabilities(),
                (),
                {},
                require_enforcement=True,
            )
            kinds = [message[0] for message in conn.sent]
            assert protocol.FAILED in kinds, (
                "the captured function reference should still report the real, "
                f"unenforced result on this platform, not the plugin's forged one - got {kinds}"
            )
            assert protocol.DONE not in kinds, f"a DONE message means the attack succeeded: {kinds}"
        finally:
            enforcement.apply_capability_enforcement = real_apply_capability_enforcement

    def test_require_enforcement_refuses_before_loading_when_the_preimport_filter_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MEDIUM-1 (round-3 review finding): apply_preimport_enforcement
        used to return None, so its own outcome was never checked - a
        container-profile-blocked install at THIS earlier point (the case
        require_enforcement was built for) let the plugin's module import/
        constructor run with no seccomp filter at all, undetected. Now
        returns its own EnforcementResult, checked here before
        _load_plugin_instance ever runs - the earliest point at which no
        plugin code has executed and the result is fully trustworthy."""
        load_calls: list[str] = []

        class _FakeConn:
            def __init__(self) -> None:
                self.sent: list[tuple[Any, ...]] = []

            def send(self, message: tuple[Any, ...]) -> None:
                self.sent.append(message)

        monkeypatch.setattr(
            runner,
            "_load_plugin_instance",
            lambda name: load_calls.append(name) or object(),  # never expected to run
        )
        monkeypatch.setattr(
            enforcement,
            "apply_preimport_enforcement",
            lambda caps: enforcement.EnforcementResult(applied=False, reason="simulated"),
        )

        conn = _FakeConn()
        runner._child_main(
            conn,
            "irrelevant-entry-point",
            "export",
            PluginCapabilities(),
            (),
            {},
            require_enforcement=True,
        )

        assert load_calls == [], "the plugin must never be loaded at all in this case"
        kinds = [message[0] for message in conn.sent]
        assert protocol.FAILED in kinds, f"expected a FAILED message, got {kinds}"
        assert protocol.ENFORCEMENT not in kinds, (
            "this refusal happens before the (post-load) ENFORCEMENT message would ever be "
            f"sent - got {kinds}"
        )


class TestPluginsOwnExceptionsPropagateCorrectly:
    """Round-3 review finding: round-2's own fix for malformed-message
    handling wrapped _handle_one_message in a broad except clause that
    also caught the FAILED branch's raise unwire_exception(...) - since a
    plugin's own ValueError/TypeError/KeyError/IndexError all cross the
    boundary as themselves (they're on RestrictedUnpickler's allowlist),
    they were mislabelled as "malformed message... terminated" instead of
    propagating as the real, documented exception a caller's own
    except ValueError/except TypeError already expects."""

    def test_rdf_exporter_value_error_propagates_as_itself(self, kb: Ontology) -> None:
        loaded = PluginRegistry(kb).register("rdf-owl-exporter", author=ADMIN)
        with pytest.raises(ValueError, match="no schema registered") as exc_info:
            loaded.instance.export(loaded.view, io.StringIO())
        assert not isinstance(exc_info.value, PluginError)

    def test_json_exporter_type_error_propagates_as_itself(self, kb: Ontology) -> None:
        loaded = PluginRegistry(kb).register("json-exporter", author=ADMIN)
        with pytest.raises(TypeError, match="Unsupported JSON export target type"):
            loaded.instance.export(loaded.view, 12345)


class TestRoundThreeDispatchLoopHardening:
    """Direct reproduction of the round-3-found regressions in round-2's
    own fixes, and the two new issues round 3 found - all against the
    real _dispatch_loop, a real spawned child, and a real pipe."""

    def test_forged_enforcement_true_does_not_suppress_a_later_genuine_false(
        self, kb: Ontology
    ) -> None:
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        process = ctx.Process(
            target=_child_forges_enforcement_true_then_genuine_false, args=(child_conn,)
        )
        process.start()
        child_conn.close()
        sink = RecordingObservabilitySink()
        try:
            result = runner._dispatch_loop(parent_conn, process, {}, "import_", sink)
            assert result == "ok"
        finally:
            parent_conn.close()
            process.join(timeout=5)

        assert len(sink.logs) == 1
        _, message, _ = sink.logs[0]
        assert "genuine failure" in message

    def test_a_flood_of_genuine_false_still_warns_only_once(self, kb: Ontology) -> None:
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        process = ctx.Process(target=_child_floods_genuine_enforcement_false, args=(child_conn,))
        process.start()
        child_conn.close()
        sink = RecordingObservabilitySink()
        try:
            runner._dispatch_loop(parent_conn, process, {}, "import_", sink)
        finally:
            parent_conn.close()
            process.join(timeout=5)

        assert len(sink.logs) == 1

    def test_empty_message_does_not_hang_the_parent(self, kb: Ontology) -> None:
        """restricted_loads(b"") raises EOFError - indistinguishable from
        a closed pipe at that layer - but a child that sends empty bytes
        and then keeps running is still alive. The old EOF handler did a
        bare, unbounded process.join(), which would hang here forever."""
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        process = ctx.Process(target=_child_sends_empty_message_then_sleeps, args=(child_conn,))
        process.start()
        child_conn.close()
        start = time.monotonic()
        try:
            with pytest.raises(PluginError, match="ended unexpectedly"):
                runner._dispatch_loop(parent_conn, process, {}, "import_", NullObservabilitySink())
        finally:
            parent_conn.close()
            process.join(timeout=5)
        elapsed = time.monotonic() - start

        assert elapsed < 10, f"parent blocked for {elapsed:.1f}s - should be reaped promptly"
        assert not process.is_alive()

    def test_corrupt_pickle_bytes_is_rejected_and_child_reaped(self, kb: Ontology) -> None:
        """Not pickle.UnpicklingError specifically - a corrupt protocol
        byte raises a plain ValueError from inside pickle's own decoder,
        uncaught by a narrower except clause."""
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        process = ctx.Process(target=_child_sends_corrupt_pickle_bytes, args=(child_conn,))
        process.start()
        child_conn.close()
        try:
            with pytest.raises(PluginError, match="malformed or disallowed"):
                runner._dispatch_loop(parent_conn, process, {}, "import_", NullObservabilitySink())
        finally:
            parent_conn.close()
            process.join(timeout=5)
        assert not process.is_alive()


class TestToWireResultDictKeys:
    """Round-3 review finding: to_wire_result's own list/tuple branch
    always returns a list (JSON has no tuple type), so reducing a dict key
    through that same function turned a perfectly safe, hashable tuple key
    into an unhashable list and crashed with a raw TypeError."""

    def test_tuple_key_survives_reduction(self) -> None:
        from ontolith.plugins.sandbox.wire import to_wire_result

        result = to_wire_result({(1, 2): "v", "plain": "w"})
        assert result == {(1, 2): "v", "plain": "w"}

    def test_nested_tuple_key_survives_reduction(self) -> None:
        from ontolith.plugins.sandbox.wire import to_wire_result

        result = to_wire_result({(1, (2, 3)): "nested"})
        assert result == {(1, (2, 3)): "nested"}

    def test_genuinely_unhashable_shaped_key_raises_cleanly(self) -> None:
        from ontolith.plugins.sandbox.wire import to_wire_result

        class _Hashable:
            def __hash__(self) -> int:
                return 1

        with pytest.raises(UnwirableArgumentError):
            to_wire_result({_Hashable(): "v"})


class TestRoundFourDispatchLoopSendSideHardening:
    """Round-4 review finding: rounds 2-3 hardened every path where the
    parent *reads* from the child; the *send* side (a reply to a CALL)
    was never wrapped, so a child that crashed or closed its pipe before
    reading a reply surfaced as a raw BrokenPipeError, not PluginError,
    and was never actively reaped by _terminate_and_join."""

    def test_child_crashing_before_reading_a_reply_surfaces_as_plugin_error(
        self, kb: Ontology
    ) -> None:
        view = ReadOnlyView(kb, ADMIN)
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        process = ctx.Process(target=_child_calls_then_closes_its_pipe, args=(child_conn,))
        process.start()
        child_conn.close()
        time.sleep(0.3)  # let the child send + close before dispatching
        try:
            with pytest.raises(PluginError, match="Communication with the plugin process failed"):
                runner._dispatch_loop(
                    parent_conn, process, {"kb": view}, "import_", NullObservabilitySink()
                )
        finally:
            parent_conn.close()
            process.join(timeout=5)
        assert not process.is_alive()

    def test_unknown_protocol_kind_is_reaped_not_just_raised(self, kb: Ontology) -> None:
        """The "unknown protocol kind" branch raises PluginError directly
        from inside _handle_one_message - round 4 found this skipped
        _terminate_and_join entirely, leaving the child alive."""
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        process = ctx.Process(target=_child_sends_unknown_protocol_kind, args=(child_conn,))
        process.start()
        child_conn.close()
        try:
            with pytest.raises(PluginError, match="Communication with the plugin process failed"):
                runner._dispatch_loop(parent_conn, process, {}, "import_", NullObservabilitySink())
        finally:
            parent_conn.close()
            process.join(timeout=5)
        assert not process.is_alive()
