"""Marshalling helpers for crossing the plugin sandbox's process boundary
(ADR-0051).

Three kinds of value cross the IPC pipe between a sandboxed plugin's child
process and the parent that hosts the real `Ontology`:

1. Plain, picklable data (paths, strings, dicts, lists, frozen dataclasses
   like `Entity`/`Assertion`) — pickled directly, no special handling.
2. A `ReadOnlyView`/`WriteView` argument — never pickled (it holds a live
   backend reference, and pickling it would defeat the entire sandbox even
   if it worked); replaced with a `RemoteViewMarker` the child resolves
   into a `RemoteReadOnlyView`/`RemoteWriteView` proxy.
3. An argument that isn't picklable but exposes a callable `.write`
   (e.g. `io.StringIO`, an open file handle) — replaced with a marker the
   child resolves into a `RemoteWritable` proxy. `.write`-shaped values are
   always proxied this way even when they *are* picklable (`io.StringIO`
   pickles successfully, into a disconnected copy — writing to that copy
   inside the child would never become visible on the caller's own
   object).

Exceptions cross the same pipe via `wire_exception`/`unwire_exception`,
pickled directly wherever possible — `OntolithError`'s `(message, detail)`
state survives a plain pickle round-trip correctly (`BaseException`'s own
`__reduce__` includes `__dict__` as reconstruction state, not just
`self.args`, so `message`/`detail` come back exactly as they went in; no
special-casing needed for this hierarchy specifically). Only a third-party
exception type that isn't picklable at all needs a fallback — see
`wire_exception`'s own docstring.
"""

from __future__ import annotations

# pickle is only ever used here to test picklability (dumps, never loads
# untrusted bytes) and to let multiprocessing's own Connection.send()/recv()
# serialize values between this project's own parent/child processes - not
# deserializing data from any external or untrusted source.
import pickle  # nosec B403
from dataclasses import dataclass

from ontolith.plugins.views import ReadOnlyView, WriteView


@dataclass(frozen=True)
class RemoteViewMarker:
    """Marks where a ReadOnlyView/WriteView argument was, before wire()."""

    tag: str
    writable: bool


@dataclass(frozen=True)
class RemoteWritableMarker:
    """Marks where a non-picklable `.write`-shaped argument was, before wire()."""

    tag: str


class UnwirableArgumentError(TypeError):
    """A plugin call argument can't cross the sandbox's process boundary.

    Raised in the parent, before any child process is spawned, when an
    argument is neither picklable nor `.write`-shaped.
    """


def _is_picklable(value: object) -> bool:
    """Return True if `value` survives a pickle round-trip attempt."""
    try:
        pickle.dumps(value)
    except Exception:
        return False
    return True


def wire_value(value: object, tag: str) -> object:
    """Replace `value` with a wire-safe marker if it can't cross by value.

    Args:
        value: The argument as the caller passed it.
        tag: A unique label for this argument's position in the call,
            reused for every message a remote proxy sends back about it
            (e.g. "kb", "arg_1").

    Returns:
        `value` unchanged if it's a plain, picklable value; a
        `RemoteViewMarker` if it's a `ReadOnlyView`/`WriteView`
        (always marshals successfully); a `RemoteWritableMarker` if it's
        neither picklable nor a view but exposes a callable `.write`.

    Raises:
        UnwirableArgumentError: `value` is none of the above — neither
            picklable, a view, nor `.write`-shaped.
    """
    if isinstance(value, ReadOnlyView):
        return RemoteViewMarker(tag=tag, writable=isinstance(value, WriteView))
    # .write-shaped values are always proxied, checked *before* picklability
    # — io.StringIO (the documented target= form JsonExporter/RdfExporter
    # support) pickles successfully, but into a disconnected copy: writing
    # to that copy inside the child would never become visible on the
    # caller's own object, silently breaking the entire reason a caller
    # passes a writable buffer instead of a path in the first place.
    if hasattr(value, "write") and callable(value.write):
        return RemoteWritableMarker(tag=tag)
    if _is_picklable(value):
        return value
    raise UnwirableArgumentError(
        f"Argument of type {type(value).__name__!r} cannot cross the plugin sandbox "
        "boundary: it is not picklable and has no callable .write method. Pass a path, "
        "a plain picklable value, or a writable object instead, or register this plugin "
        "with isolate=False to opt out of sandboxing."
    )


def wire_exception(exc: BaseException) -> object:
    """Marshal `exc` for crossing the boundary.

    Returns:
        `exc` itself if it's picklable (true of every `OntolithError`
        subclass and every ordinary builtin exception — `message`/`detail`
        survive intact, see this module's own docstring); a plain
        `RuntimeError` carrying `exc`'s original type name and message
        otherwise (a third-party exception type that isn't picklable at
        all — a narrow, documented fidelity loss, see ADR-0051's
        Consequences).
    """
    if _is_picklable(exc):
        return exc
    return RuntimeError(f"{type(exc).__name__}: {exc}")


def unwire_exception(wired: object) -> BaseException:
    """Reverse `wire_exception` — reconstructs the original exception (or
    the closest faithful equivalent) in the receiving process."""
    if isinstance(wired, BaseException):
        return wired
    return RuntimeError(f"Malformed wired exception: {wired!r}")


__all__ = [
    "RemoteViewMarker",
    "RemoteWritableMarker",
    "UnwirableArgumentError",
    "wire_value",
    "wire_exception",
    "unwire_exception",
]
