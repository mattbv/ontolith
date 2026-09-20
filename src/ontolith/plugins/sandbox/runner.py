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

import multiprocessing
from importlib.metadata import entry_points
from typing import Any

from ontolith.core.errors import PluginError
from ontolith.plugins.manifest import PluginCapabilities, PluginKind, PluginManifest
from ontolith.plugins.sandbox import enforcement, protocol
from ontolith.plugins.sandbox.remote_view import RemoteReadOnlyView, RemoteWritable, RemoteWriteView
from ontolith.plugins.sandbox.wire import (
    RemoteViewMarker,
    RemoteWritableMarker,
    unwire_exception,
    wire_exception,
    wire_value,
)

_ENTRY_POINT_GROUP = "ontolith.plugins"

_ENTRYPOINT_METHOD_BY_KIND: dict[PluginKind, str] = {
    "importer": "import_",
    "exporter": "export",
    "reasoner": "derive",
    "validator": "validate",
    "connector": "sync",
}


class IsolatedPluginProxy:
    """`LoadedPlugin.instance` when a plugin is registered with
    `isolate=True` (the default) — runs its one protocol method in a
    sandboxed child process instead of the caller's own process.
    """

    def __init__(self, entry_point_name: str, manifest: PluginManifest, plugin_class: type) -> None:
        self._entry_point_name = entry_point_name
        self._manifest = manifest
        self.plugin_class = plugin_class
        method_name = _ENTRYPOINT_METHOD_BY_KIND[manifest.kind]
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
                self._entry_point_name, method_name, self._manifest.capabilities, args, kwargs
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

    Returns:
        Whatever the plugin's real method returned.

    Raises:
        UnwirableArgumentError: an argument can't cross the boundary.
        PluginError: the child process ended without completing the call
            (crashed, was killed, or its own result/exception couldn't be
            marshaled back).
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
        return _dispatch_loop(parent_conn, process, real_objects, method_name)
    finally:
        parent_conn.close()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join()


def _dispatch_loop(
    parent_conn: Any, process: Any, real_objects: dict[str, object], method_name: str
) -> object:
    """Service CALL messages against `real_objects` until DONE/FAILED."""
    while True:
        try:
            message = parent_conn.recv()
        except EOFError as exc:
            process.join()
            raise PluginError(
                f"Plugin process ended unexpectedly (exit code {process.exitcode}) before "
                f"completing {method_name!r} — it may have crashed or been terminated."
            ) from exc

        kind = message[0]
        if kind == protocol.CALL:
            _, tag, call_method_name, call_args, call_kwargs = message
            real_obj = real_objects.get(tag)
            if real_obj is None:
                protocol.send_error(
                    parent_conn, wire_exception(PluginError(f"Unknown remote object tag {tag!r}"))
                )
                continue
            try:
                result = getattr(real_obj, call_method_name)(*call_args, **call_kwargs)
                protocol.send_result(parent_conn, result)
            except Exception as exc:  # noqa: BLE001 - forwarded to the child as-is
                protocol.send_error(parent_conn, wire_exception(exc))
        elif kind == protocol.DONE:
            return message[1]
        elif kind == protocol.FAILED:
            raise unwire_exception(message[1])
        else:  # pragma: no cover - defensive, protocol is closed and internal
            raise PluginError(f"Unknown sandbox protocol message kind: {kind!r}")


def _resolve_wired(value: object, child_conn: Any) -> object:
    """Reverse `_wire_all`'s substitution inside the child."""
    if isinstance(value, RemoteViewMarker):
        if value.writable:
            return RemoteWriteView(child_conn, value.tag)
        return RemoteReadOnlyView(child_conn, value.tag)
    if isinstance(value, RemoteWritableMarker):
        return RemoteWritable(child_conn, value.tag)
    return value


def _load_plugin_instance(entry_point_name: str) -> Any:
    """Resolve and instantiate the plugin class registered under
    `entry_point_name`, independently of PluginRegistry's own copy of this
    logic — deliberately not shared, to avoid a circular import between
    `registry.py` (which needs IsolatedPluginProxy) and this module (which
    would otherwise need a helper from registry.py)."""
    matches = [ep for ep in entry_points(group=_ENTRY_POINT_GROUP) if ep.name == entry_point_name]
    if not matches:
        raise PluginError(f"Plugin entry point not found: {entry_point_name!r}")
    plugin_class = matches[0].load()
    return plugin_class()


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
        protocol.send_failed(child_conn, wire_exception(exc))
        return

    # Best-effort, never raises - see enforcement.py's own docstring for why
    # a failure to enforce degrades to "not enforced," not a hard error.
    enforcement.apply_capability_enforcement(capabilities)

    try:
        result = getattr(plugin_instance, method_name)(*resolved_args, **resolved_kwargs)
    except Exception as exc:  # noqa: BLE001 - reported to the parent, not raised here
        protocol.send_failed(child_conn, wire_exception(exc))
        return

    try:
        protocol.send_done(child_conn, result)
    except Exception:  # noqa: BLE001 - result itself isn't picklable
        protocol.send_failed(
            child_conn,
            wire_exception(
                PluginError(
                    f"{method_name}()'s return value ({type(result).__name__}) is not "
                    "picklable and cannot cross the plugin sandbox boundary"
                )
            ),
        )


__all__ = ["IsolatedPluginProxy", "run_isolated"]
