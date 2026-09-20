"""Marshalling helpers for crossing the plugin sandbox's process boundary
(ADR-0051).

**The child process is untrusted; the parent process is not.** Every value
this module handles is classified by which direction it crosses, because
the safety rules are asymmetric:

- Parent -> child (the initial call's `args`/`kwargs`, and every `RESULT`/
  `ERROR` reply to a proxied view call): produced by already-trusted code
  (`register()` requires `admin` capability; nothing in `interfaces/*`
  reaches a plugin — see ADR-0051). Plain `pickle` is fine here.
- Child -> parent (every `CALL` a plugin's own code makes through its `kb`
  view, and the plugin's own final return value or raised exception): the
  bytes on this side of the pipe are attacker-controlled, full stop. A
  plugin need not even use the `RemoteReadOnlyView`/`RemoteWriteView`
  wrappers this module provides to reach the parent's `recv()` — it has
  the raw pipe file descriptor and can write a hand-crafted, hostile pickle
  stream directly, regardless of what "well-behaved" client code would
  send. Standard `pickle.loads()` on this bytes is unconditionally unsafe:
  a `REDUCE` opcode can invoke *any* importable callable with attacker-
  chosen arguments the moment `.loads()` runs, before any of this
  project's own code executes. This is why `runner.py`'s dispatch loop
  reads this side via `restricted_loads()` (below), never plain
  `Connection.recv()`.

`wire_value`/`to_wire_result` classify and, where needed, convert *values*
before they're ever pickled — `ReadOnlyView`/`WriteView` arguments become
`RemoteViewMarker`s (never pickled: the object holds a live backend
reference, and a plugin's own code must never be handed one), `.write`-
shaped-but-unpicklable arguments become `RemoteWritableMarker`s, and a
plugin's own return value is reduced to a JSON-safe shape (`to_wire_result`)
so `restricted_loads` never has to reconstruct an arbitrary, plugin-defined
class on the parent side. `restricted_loads`/`RestrictedUnpickler` are the
actual security boundary for the child -> parent direction: only a small,
explicit allowlist of `ontolith.core.errors` exception classes (plus a
short list of ordinary builtin exceptions) may be reconstructed as a class
instance; everything else must arrive as one of `NoneType`/`bool`/`int`/
`float`/`str`/`bytes`/`list`/`dict`/`tuple`, which pickle's own opcodes
handle without ever calling `find_class` at all.
"""

from __future__ import annotations

import dataclasses
import io

# pickle.dumps() (encoding outbound data) is safe regardless of direction -
# the risk is in *decoding* bytes from the untrusted child, which
# RestrictedUnpickler below exists specifically to contain. Plain
# pickle.loads()/Unpickler are never used on that side of the pipe.
import pickle  # nosec B403
from typing import Any

from ontolith.core.errors import OntolithError
from ontolith.plugins.views import ReadOnlyView, WriteView

_JSON_SAFE_SCALARS: tuple[type, ...] = (type(None), bool, int, float, str, bytes)


def _ontolith_error_hierarchy() -> frozenset[tuple[str, str]]:
    """Every OntolithError subclass, found by introspection (`__subclasses__`)
    rather than a naming convention — `PolicyDenied` doesn't end in
    "Error" despite being one, which a name-pattern filter would silently
    drop from the allowlist below. `core/errors.py`'s hierarchy is a
    single level deep (verified: none of OntolithError's own subclasses
    have subclasses of their own), but this still walks recursively so a
    future deeper subclass isn't silently excluded either."""
    found: set[type[BaseException]] = {OntolithError}
    frontier = [OntolithError]
    while frontier:
        current = frontier.pop()
        for subclass in current.__subclasses__():
            if subclass not in found:
                found.add(subclass)
                frontier.append(subclass)
    return frozenset((cls.__module__, cls.__name__) for cls in found)


# Exception classes RestrictedUnpickler may reconstruct as a real instance -
# every OntolithError subclass (core/errors.py, all sharing its one
# (message, detail) constructor shape) plus a short, deliberately narrow
# list of ordinary builtin exceptions a plugin's own code plausibly raises
# without going through this project's taxonomy at all (a bare ValueError,
# a PermissionError from a denied seccomp syscall, etc.). Nothing else -
# in particular, no plugin-defined exception type - may cross as itself;
# wire_exception's own fallback already turns anything not on this list
# into a plain RuntimeError before it's even sent.
_SAFE_EXCEPTION_CLASSES: frozenset[tuple[str, str]] = _ontolith_error_hierarchy() | frozenset(
    {
        ("builtins", name)
        for name in (
            "Exception",
            "ValueError",
            "TypeError",
            "RuntimeError",
            "KeyError",
            "IndexError",
            "AttributeError",
            "PermissionError",
            "OSError",
            "StopIteration",
        )
    }
)


@dataclasses.dataclass(frozen=True)
class RemoteViewMarker:
    """Marks where a ReadOnlyView/WriteView argument was, before wire().

    Carries `principal_id` too (a plain string, already known to the
    trusted parent when the marker is built) so the child-side proxy can
    expose the real view's own `principal_id` property with no round trip
    — parity with `ReadOnlyView`'s public surface (a plugin using
    `principal_id` to stamp e.g. `source=` broke under isolation before
    this field existed).
    """

    tag: str
    writable: bool
    principal_id: str


@dataclasses.dataclass(frozen=True)
class RemoteWritableMarker:
    """Marks where a non-picklable `.write`-shaped argument was, before wire()."""

    tag: str


class UnwirableArgumentError(TypeError):
    """A value can't cross the sandbox's process boundary.

    Raised in the parent (before any child process is spawned) for a call
    argument that's neither picklable, a view, nor `.write`-shaped; raised
    in the child (before any bytes are sent) for a plugin's own return
    value that isn't reducible to a JSON-safe shape.
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

    Parent -> child only (an initial call's `args`/`kwargs`): the caller
    is already-trusted code, so a plain, already-JSON-unsafe-but-picklable
    value (e.g. an `Entity` a caller happens to pass through) is left
    alone here rather than restricted the way a child -> parent value is —
    see this module's own docstring for why the two directions have
    different rules.

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
        return RemoteViewMarker(
            tag=tag, writable=isinstance(value, WriteView), principal_id=value.principal_id
        )
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


def to_wire_result(value: object) -> object:
    """Reduce a plugin's own return value (child -> parent) to a JSON-safe
    shape: `None`/`bool`/`int`/`float`/`str`/`bytes` pass through; `list`/
    `tuple`/`dict` recurse; a `@dataclass` instance (e.g. a reference
    plugin's `ImportReport`/`ExportReport`) becomes a plain `dict` of its
    fields via `dataclasses.asdict()` (itself recursive).

    This is the reason a plugin's dataclass-shaped return value crosses an
    isolated call as a `dict`, not its original type — `isolate=False`
    still returns the real object unchanged (see ADR-0051's Consequences).
    The restriction exists so `restricted_loads` (below) never has to
    reconstruct an arbitrary, plugin-defined class on the parent side —
    only this project's own known-safe exception hierarchy ever needs
    `find_class` to resolve a class at all.

    Raises:
        UnwirableArgumentError: `value` is not JSON-safe and not a
            dataclass instance — a plugin returning, say, a raw file
            handle or a third-party ORM object.
    """
    if isinstance(value, _JSON_SAFE_SCALARS):
        return value
    if isinstance(value, (list, tuple)):
        return [to_wire_result(item) for item in value]
    if isinstance(value, dict):
        return {key: to_wire_result(item) for key, item in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_wire_result(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    raise UnwirableArgumentError(
        f"A plugin's return value of type {type(value).__name__!r} cannot cross the "
        "plugin sandbox boundary: only None/bool/int/float/str/bytes, list/tuple/dict "
        "of the same, and @dataclass instances (converted to a plain dict) are "
        "permitted. Register this plugin with isolate=False if it must return "
        "something else."
    )


def wire_exception(exc: BaseException) -> object:
    """Marshal `exc` for crossing the boundary.

    Returns:
        `exc` itself if it's picklable *and* on `restricted_loads`'s
        exception allowlist (true of every `OntolithError` subclass and a
        short list of ordinary builtin exceptions — `message`/`detail`
        survive a plain pickle round-trip intact, since `BaseException`'s
        own `__reduce__` includes `__dict__`, not just `self.args`); a
        plain `RuntimeError` carrying `exc`'s original type name and
        message otherwise (a third-party exception type that isn't on the
        allowlist, whether or not it happens to be picklable — a narrow,
        documented fidelity loss, see ADR-0051's Consequences).
    """
    allowed = {(cls.__module__, cls.__name__) for cls in (type(exc), *type(exc).__mro__)}
    if allowed & _SAFE_EXCEPTION_CLASSES and _is_picklable(exc):
        return exc
    return RuntimeError(f"{type(exc).__name__}: {exc}")


def unwire_exception(wired: object) -> BaseException:
    """Reverse `wire_exception` — reconstructs the original exception (or
    the closest faithful equivalent) in the receiving process. Parent ->
    child only (a proxied view call's ERROR reply); the child -> parent
    direction is decoded via `restricted_loads`, not this function."""
    if isinstance(wired, BaseException):
        return wired
    return RuntimeError(f"Malformed wired exception: {wired!r}")


class RestrictedUnpickler(pickle.Unpickler):
    """Deserializes bytes the untrusted child sent, refusing to construct
    any class instance not on `_SAFE_EXCEPTION_CLASSES`.

    `find_class` is the entire attack surface of `pickle.loads()`: a
    `GLOBAL`/`STACK_GLOBAL` opcode resolves an arbitrary `module.name`
    reference — a class *or a plain function* — and a `REDUCE` opcode can
    then call it with attacker-chosen arguments before any of this
    project's own code runs. Restricting `find_class` to a short, explicit
    allowlist closes that regardless of what a plugin's raw bytes contain;
    `None`/`bool`/`int`/`float`/`str`/`bytes`/`list`/`dict`/`tuple` never
    reach `find_class` at all (dedicated opcodes handle them), which is
    why `to_wire_result` reduces everything else to those shapes before
    it's ever sent.
    """

    def find_class(self, module: str, name: str) -> Any:
        """Resolve `module.name` only if it's on `_SAFE_EXCEPTION_CLASSES` —
        refuse (and thereby refuse the whole containing pickle stream)
        otherwise. See this class's own docstring for why this one check
        is the entire security boundary here."""
        if (module, name) in _SAFE_EXCEPTION_CLASSES:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(
            f"Refusing to reconstruct {module}.{name} from untrusted sandbox input "
            "(not on the exception allowlist) — plugins/sandbox/wire.py"
        )


def restricted_loads(data: bytes) -> object:
    """Safely deserialize bytes received from the untrusted child process.

    Use for every message read from the child's end of the sandbox pipe —
    never plain `Connection.recv()`/`pickle.loads()` on that side. See
    this module's own docstring for the full parent/child trust asymmetry.
    """
    return RestrictedUnpickler(io.BytesIO(data)).load()


__all__ = [
    "RemoteViewMarker",
    "RemoteWritableMarker",
    "RestrictedUnpickler",
    "UnwirableArgumentError",
    "restricted_loads",
    "to_wire_result",
    "wire_value",
    "wire_exception",
    "unwire_exception",
]
