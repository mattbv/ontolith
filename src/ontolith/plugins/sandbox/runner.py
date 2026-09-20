"""Parent-side orchestration and child-process entrypoint for a sandboxed
plugin call (ADR-0051).

`IsolatedPluginProxy` is what `PluginRegistry.register()` returns as
`LoadedPlugin.instance` when `isolate=True` (the default). It exposes
exactly one method — whichever `plugins/ports.py` protocol method
`manifest.kind` implies (`import_`/`export`/`derive`/`validate`/`sync`) —
which spawns a fresh child process, marshals arguments across
(`sandbox/wire.py`), proxies any `kb` view or writable target back to the
parent (`sandbox/remote_view.py`) over one shared pipe
(`sandbox/protocol.py`), and returns or raises exactly what the real,
unsandboxed call would have.

A fresh process per call, not a long-lived worker reused across calls — see
ADR-0051's Rationale for why. `multiprocessing.get_context("spawn")` is
used unconditionally: `fork` is POSIX-only and unsafe to mix with threads/
locks, and `spawn` is what this project's own CI matrix (ubuntu/macos/
windows) needs to behave identically everywhere.
"""

from __future__ import annotations

import logging
import multiprocessing

# Referenced only for pickle.UnpicklingError (a plain exception class) - see
# protocol.recv_from_child's own docstring for where untrusted bytes are
# actually decoded (wire.restricted_loads), not here.
import pickle  # nosec B403
from importlib.metadata import entry_points
from typing import Any

from ontolith.core.errors import PluginError
from ontolith.core.observability import ObservabilitySink
from ontolith.plugins.manifest import PluginCapabilities, PluginKind, PluginManifest
from ontolith.plugins.sandbox import enforcement, protocol
from ontolith.plugins.sandbox.remote_view import RemoteReadOnlyView, RemoteWritable, RemoteWriteView
from ontolith.plugins.sandbox.wire import (
    RemoteViewMarker,
    RemoteWritableMarker,
    to_wire_result,
    unwire_exception,
    wire_exception,
    wire_value,
)
from ontolith.plugins.views import ReadOnlyView, WriteView

_ENTRY_POINT_GROUP = "ontolith.plugins"

_ENTRYPOINT_METHOD_BY_KIND: dict[PluginKind, str] = {
    "importer": "import_",
    "exporter": "export",
    "reasoner": "derive",
    "validator": "validate",
    "connector": "sync",
}

# The dispatch loop's own allow-list — the security boundary CRITICAL-2
# (security review) exists to close. A malicious plugin's CALL message
# carries a method_name string it fully controls; without this, a bare
# getattr(real_view, call_method_name)(*call_args, **call_kwargs) would
# invoke *anything* on the parent's real, unproxied ReadOnlyView/WriteView
# — including __setattr__("__class__", WriteView) to retype a read-only
# view into a writable one, or __setattr__("_principal_id", "admin@...")
# to forge the acting principal on every subsequent write through that
# view. Restricting to exactly RemoteReadOnlyView/RemoteWriteView's own
# public method set (never a dunder, never query()/as_of() — see below)
# closes both: only these names are ever dispatched, regardless of what a
# hand-crafted, protocol-bypassing message asks for.
_READ_ONLY_VIEW_METHODS: frozenset[str] = frozenset({"get_entity", "schema", "assertions"})
_WRITE_VIEW_METHODS: frozenset[str] = _READ_ONLY_VIEW_METHODS | frozenset(
    {"create_entity", "propose", "propose_ref", "retract"}
)
_WRITABLE_METHODS: frozenset[str] = frozenset({"write"})
# query()/as_of() are deliberately excluded even though they're real
# ReadOnlyView methods: they return a QueryBuilder/AsOfView holding a live
# backend reference, which send_result would then pickle and hand straight
# to the plugin — exactly the "live object graph" ADR-0051 exists to keep
# out of reach. RemoteReadOnlyView.query()/.as_of() never send a CALL for
# these (see remote_view.py), and this allow-list refuses them too, so a
# message bypassing that proxy entirely gets the same refusal.


def _allowed_methods_for(real_obj: object) -> frozenset[str]:
    """The method names the dispatch loop will actually invoke on
    `real_obj` — never a bare, unrestricted getattr (see the allow-list
    comment above)."""
    if isinstance(real_obj, WriteView):
        return _WRITE_VIEW_METHODS
    if isinstance(real_obj, ReadOnlyView):
        return _READ_ONLY_VIEW_METHODS
    if hasattr(real_obj, "write"):
        return _WRITABLE_METHODS
    return frozenset()


class IsolatedPluginProxy:
    """`LoadedPlugin.instance` when a plugin is registered with
    `isolate=True` (the default) — runs its one protocol method in a
    sandboxed child process instead of the caller's own process.
    """

    def __init__(
        self,
        entry_point_name: str,
        manifest: PluginManifest,
        plugin_class: type,
        observability: ObservabilitySink,
    ) -> None:
        self._entry_point_name = entry_point_name
        self._manifest = manifest
        self.plugin_class = plugin_class
        self._observability = observability
        try:
            method_name = _ENTRYPOINT_METHOD_BY_KIND[manifest.kind]
        except KeyError as exc:
            # Matches registry.py's own precedent for an unmapped PluginKind
            # (_WRITE_CAPABLE_KINDS' comment) - a bare KeyError here would be
            # a confusing way to say "this pass doesn't yet know how to
            # isolate this plugin kind."
            raise PluginError(
                f"No sandboxed entrypoint mapping for plugin kind {manifest.kind!r} "
                "(ADR-0051) — register this plugin with isolate=False."
            ) from exc
        setattr(self, method_name, self._make_bound_call(method_name))

    def _make_bound_call(self, method_name: str) -> Any:
        """Build the one bound method (named `method_name`) this proxy
        exposes, closing over `self` so `run_isolated` sees this
        registration's own entry point and capabilities."""

        def _invoke(*args: object, **kwargs: object) -> object:
            """Sandboxed call to the underlying plugin's protocol method
            (ADR-0051) — runs in a fresh child process; overwritten below
            with a name-specific docstring for runtime introspection."""
            return run_isolated(
                self._entry_point_name,
                method_name,
                self._manifest.capabilities,
                args,
                kwargs,
                self._observability,
            )

        _invoke.__name__ = method_name
        _invoke.__doc__ = (
            f"Sandboxed call to the underlying plugin's {method_name}() (ADR-0051) — "
            "runs in a fresh child process; see IsolatedPluginProxy's own docstring."
        )
        return _invoke


def _wire_all(
    args: tuple[object, ...], kwargs: dict[str, object]
) -> tuple[tuple[object, ...], dict[str, object], dict[str, object]]:
    """Replace each view/non-picklable argument with a wire marker.

    Returns:
        (wired_args, wired_kwargs, real_objects) — real_objects maps each
        substituted argument's tag to the actual object the parent's
        dispatch loop should call methods on.
    """
    real_objects: dict[str, object] = {}

    wired_args = []
    for index, value in enumerate(args):
        tag = f"arg_{index}"
        wired = wire_value(value, tag)
        if wired is not value:
            real_objects[tag] = value
        wired_args.append(wired)

    wired_kwargs: dict[str, object] = {}
    for name, value in kwargs.items():
        tag = f"kwarg_{name}"
        wired = wire_value(value, tag)
        if wired is not value:
            real_objects[tag] = value
        wired_kwargs[name] = wired

    return tuple(wired_args), wired_kwargs, real_objects


def run_isolated(
    entry_point_name: str,
    method_name: str,
    capabilities: PluginCapabilities,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    observability: ObservabilitySink,
) -> object:
    """Run `method_name(*args, **kwargs)` on a fresh instance of the plugin
    registered under `entry_point_name`, in a sandboxed child process.

    Args:
        entry_point_name: Entry point to re-resolve inside the child — the
            plugin is reconstructed from scratch there, never pickled
            across from an already-constructed parent-side instance.
        method_name: The one protocol method to call (e.g. "import_").
        capabilities: The plugin's declared manifest capabilities, used to
            decide what OS-level enforcement to attempt inside the child.
        args: Positional arguments as the caller passed them — a
            ReadOnlyView/WriteView argument is detected structurally and
            proxied; everything else must be picklable or `.write`-shaped.
        kwargs: Keyword arguments, same rules as `args`.
        observability: Logged to if this specific call's OS-level
            enforcement attempt didn't apply (e.g. seccomp is available in
            principle but failed to install for this call) — the
            registration-time warning (`registry.py`) can only predict
            this, not guarantee it.

    Returns:
        Whatever the plugin's real method returned, as decoded by
        `restricted_loads` on the parent side (round-2 review finding:
        `to_wire_result` runs in the *child*, reducing a dataclass return
        value to a JSON-safe `dict` before it's ever sent — see ADR-0051's
        Consequences — but it is not itself a parent-side control; the
        parent's only real guarantee is whatever `restricted_loads`
        accepted, which also includes an allowlisted exception *instance*
        with attacker-chosen `args`/`__dict__` if a plugin sends one as
        its own "result" rather than raising it. A caller expecting a
        `dict` should not assume it cannot instead receive an
        `Exception`).

    Raises:
        UnwirableArgumentError: an argument can't cross the boundary.
        PluginError: the child process ended without completing the call
            (crashed, was killed, or its own result/exception couldn't be
            marshaled back), or sent a malformed/disallowed message.
        Any exception the plugin's own method raised, including a denied-
            syscall OSError/PermissionError under Linux seccomp
            enforcement (ADR-0051) — reconstructed via
            sandbox.wire.unwire_exception.
    """
    wired_args, wired_kwargs, real_objects = _wire_all(args, kwargs)

    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=True)
    process = ctx.Process(
        target=_child_main,
        args=(child_conn, entry_point_name, method_name, capabilities, wired_args, wired_kwargs),
    )
    process.start()
    child_conn.close()  # only the child's copy is used inside the child

    try:
        return _dispatch_loop(parent_conn, process, real_objects, method_name, observability)
    finally:
        parent_conn.close()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join()


def _dispatch_loop(
    parent_conn: Any,
    process: Any,
    real_objects: dict[str, object],
    method_name: str,
    observability: ObservabilitySink,
) -> object:
    """Service CALL messages against `real_objects` until DONE/FAILED.

    Every message is read via `protocol.recv_from_child` (restricted
    unpickling — the child is untrusted) and every CALL's method name is
    checked against `_allowed_methods_for`'s explicit allow-list before
    `getattr` ever runs — a bare, unrestricted dispatch would let a
    hand-crafted CALL invoke anything on the parent's real, unproxied view
    object, including `__setattr__` (security review finding: this is
    what closes it).
    """
    # A single-element mutable container (not a plain bool) so
    # _handle_one_message can update it across loop iterations - only the
    # first ENFORCEMENT message is honored; a malicious child sending an
    # unbounded stream of them (nothing in the protocol otherwise limits
    # how many it may send) would otherwise flood kb.observability with
    # one WARNING per message (round-2 review finding).
    enforcement_reported = [False]

    while True:
        try:
            message = protocol.recv_from_child(parent_conn)
        except EOFError as exc:
            process.join()
            raise PluginError(
                f"Plugin process ended unexpectedly (exit code {process.exitcode}) before "
                f"completing {method_name!r} — it may have crashed or been terminated."
            ) from exc
        except pickle.UnpicklingError as exc:
            process.terminate()
            process.join()
            raise PluginError(
                f"Plugin process sent a malformed or disallowed message while "
                f"completing {method_name!r} — terminated. ({exc})"
            ) from exc

        try:
            outcome = _handle_one_message(
                message, parent_conn, real_objects, method_name, observability, enforcement_reported
            )
        except (ValueError, TypeError, IndexError, KeyError) as exc:
            # A malformed message shape (wrong arity, an unhashable tag/
            # method name, etc.) - round-2 review finding: this previously
            # escaped as a raw ValueError/TypeError/IndexError, violating
            # this function's own documented PluginError contract for "a
            # malformed or disallowed message." restricted_loads already
            # keeps the *content* of every value safe; this closes the
            # same hole for the message's own *shape*.
            process.terminate()
            process.join()
            raise PluginError(
                f"Plugin process sent a malformed message while completing {method_name!r} "
                f"— terminated. ({type(exc).__name__}: {exc})"
            ) from exc
        if outcome is not _CONTINUE:
            return outcome


_CONTINUE = object()  # sentinel: _handle_one_message wants the loop to keep going


def _handle_one_message(
    message: tuple[Any, ...],
    parent_conn: Any,
    real_objects: dict[str, object],
    method_name: str,
    observability: ObservabilitySink,
    enforcement_reported: list[bool],
) -> object:
    """Handle exactly one already-decoded message. Returns `_CONTINUE` to
    keep looping, or the isolated call's final result (from a DONE
    message). Raises `unwire_exception(...)` for a FAILED message, or lets
    a malformed message's own unpacking error (ValueError/TypeError/
    IndexError/KeyError) propagate — `_dispatch_loop`'s caller converts
    that to a PluginError, since this function only has to worry about a
    message that's well-typed per `restricted_loads` but the wrong shape.
    """
    kind = message[0]
    if kind == protocol.ENFORCEMENT:
        if enforcement_reported[0]:
            return _CONTINUE  # only the first ENFORCEMENT message is honored
        enforcement_reported[0] = True
        _, applied, reason = message
        if not applied:
            observability.log(
                logging.WARNING,
                f"OS-level capability enforcement did not apply for this call to "
                f"{method_name!r}: {reason}. capabilities.network/.filesystem are not "
                "enforced for this specific call (ADR-0051).",
                method=method_name,
            )
        return _CONTINUE
    if kind == protocol.CALL:
        _, tag, call_method_name, call_args, call_kwargs = message
        real_obj = real_objects.get(tag)
        if (
            real_obj is None
            or not isinstance(call_method_name, str)
            or call_method_name not in _allowed_methods_for(real_obj)
        ):
            protocol.send_error(
                parent_conn,
                wire_exception(
                    PluginError(
                        f"Call to {call_method_name!r} on tag {tag!r} is not permitted "
                        "inside a sandboxed plugin call"
                    )
                ),
            )
            return _CONTINUE
        try:
            result = getattr(real_obj, call_method_name)(*call_args, **call_kwargs)
            protocol.send_result(parent_conn, result)
        except Exception as exc:  # noqa: BLE001 - forwarded to the child as-is
            protocol.send_error(parent_conn, wire_exception(exc))
        return _CONTINUE
    if kind == protocol.DONE:
        return message[1]
    if kind == protocol.FAILED:
        raise unwire_exception(message[1])
    raise PluginError(f"Unknown sandbox protocol message kind: {kind!r}")


def _resolve_wired(value: object, child_conn: Any) -> object:
    """Reverse `_wire_all`'s substitution inside the child."""
    if isinstance(value, RemoteViewMarker):
        if value.writable:
            return RemoteWriteView(child_conn, value.tag, value.principal_id)
        return RemoteReadOnlyView(child_conn, value.tag, value.principal_id)
    if isinstance(value, RemoteWritableMarker):
        return RemoteWritable(child_conn, value.tag)
    return value


def _load_plugin_instance(entry_point_name: str) -> Any:
    """Resolve and instantiate the plugin class registered under
    `entry_point_name`, independently of PluginRegistry's own copy of this
    logic — deliberately not shared, to avoid a circular import between
    `registry.py` (which needs IsolatedPluginProxy) and this module (which
    would otherwise need a helper from registry.py). Mirrors
    `PluginRegistry._load_entry_point`'s error handling exactly (including
    the ambiguous-match case) so registration-time and call-time
    resolution never diverge on which entry point actually gets used."""
    matches = [ep for ep in entry_points(group=_ENTRY_POINT_GROUP) if ep.name == entry_point_name]
    if not matches:
        raise PluginError(f"Plugin entry point not found: {entry_point_name!r}")
    if len(matches) > 1:
        raise PluginError(
            f"Ambiguous plugin entry point {entry_point_name!r}: {len(matches)} distributions "
            "register this name under the 'ontolith.plugins' group"
        )
    try:
        plugin_class = matches[0].load()
    except Exception as exc:
        raise PluginError(f"Failed to load plugin {entry_point_name!r}: {exc}") from exc
    try:
        return plugin_class()
    except Exception as exc:
        raise PluginError(f"Failed to instantiate plugin {entry_point_name!r}: {exc}") from exc


def _safe_send_failed(child_conn: Any, exc: BaseException) -> None:
    """`protocol.send_failed`, swallowing a broken-pipe failure of its own —
    if the parent already closed its end (it gave up waiting, or the
    dispatch loop itself raised), there's nothing left to report to."""
    try:
        protocol.send_failed(child_conn, wire_exception(exc))
    except Exception:  # noqa: BLE001  # nosec B110
        # Genuinely nothing more to do: the parent's end is already gone, so
        # there is no one left to report to and nothing to recover - this is
        # the child process's own final action.
        pass


def _child_main(
    child_conn: Any,
    entry_point_name: str,
    method_name: str,
    capabilities: PluginCapabilities,
    wired_args: tuple[object, ...],
    wired_kwargs: dict[str, object],
) -> None:
    """Entry point for the sandboxed child process."""
    try:
        plugin_instance = _load_plugin_instance(entry_point_name)
        resolved_args = tuple(_resolve_wired(value, child_conn) for value in wired_args)
        resolved_kwargs = {
            name: _resolve_wired(value, child_conn) for name, value in wired_kwargs.items()
        }
    except Exception as exc:  # noqa: BLE001 - reported to the parent, not raised here
        _safe_send_failed(child_conn, exc)
        return

    # Best-effort, never raises - see enforcement.py's own docstring for why
    # a failure to enforce degrades to "not enforced," not a hard error.
    # Reported to the parent unconditionally (security review finding: the
    # registration-time warning can only predict this, not guarantee it —
    # e.g. a container's own outer seccomp profile can block installing a
    # filter even when pyseccomp/libseccomp are both present).
    enforcement_result = enforcement.apply_capability_enforcement(capabilities)
    try:
        protocol.send_enforcement(child_conn, enforcement_result.applied, enforcement_result.reason)
    except Exception:  # noqa: BLE001 - the pipe itself is gone; nothing left to report to
        return

    try:
        result = getattr(plugin_instance, method_name)(*resolved_args, **resolved_kwargs)
    except Exception as exc:  # noqa: BLE001 - reported to the parent, not raised here
        _safe_send_failed(child_conn, exc)
        return

    try:
        wired_result = to_wire_result(result)
    except Exception as exc:  # noqa: BLE001 - reported to the parent, not raised here
        _safe_send_failed(child_conn, exc)
        return

    try:
        protocol.send_done(child_conn, wired_result)
    except Exception:  # noqa: BLE001  # nosec B110 - the pipe itself is gone at this point
        pass


__all__ = ["IsolatedPluginProxy", "run_isolated"]
