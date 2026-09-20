"""Wire message shapes for the plugin sandbox's single duplex pipe (ADR-0051).

One `multiprocessing.Pipe()` carries every message for one isolated plugin
call. Messages are plain tuples (`multiprocessing.connection.Connection`
pickles whatever is sent) tagged by a leading string so both ends can
dispatch on it. There is never more than one request in flight at a time —
the child blocks on `recv()` after every `CALL` until it gets a matching
`RESULT`/`ERROR`, so no request/response correlation id is needed beyond the
per-argument `tag` a `CALL` message already carries (which argument's proxy
made this call), not a call-sequence id.

Child -> parent, exactly once, right after attempting OS-level capability
enforcement and before calling the plugin's own method (not answered):
    (ENFORCEMENT, applied, reason)

Child -> parent, during the plugin's own method execution (any number of
times, always answered before the child sends anything else):
    (CALL, tag, method_name, args, kwargs)

Parent -> child, one per CALL, always the very next message on the pipe:
    (RESULT, value)
    (ERROR, wired_exception)

Child -> parent, exactly once, ending the exchange:
    (DONE, wired_result)
    (FAILED, wired_exception)

**Every message read on the child's side of the pipe is untrusted** (the
child process runs the plugin's own code) — `recv_from_child` decodes it
through `wire.restricted_loads`, never plain `Connection.recv()`/
`pickle.loads()`. Messages read on the parent's side (`RESULT`/`ERROR`
replies) come from already-trusted code and use plain `Connection.recv()`
in `remote_view.py` — see `wire.py`'s own module docstring for the full
trust-direction rationale. Every `send_*` helper below uses plain
`Connection.send()` regardless of direction: *encoding* outbound data
never executes anything, only *decoding* untrusted bytes does.
"""

from __future__ import annotations

# Only pickle.UnpicklingError is referenced here (a plain exception class,
# not pickle.loads/Unpickler) - actual untrusted-bytes decoding happens in
# wire.restricted_loads, imported below.
import pickle  # nosec B403
from typing import Any, Final

from ontolith.plugins.sandbox.wire import restricted_loads

CALL: Final = "call"
RESULT: Final = "result"
ERROR: Final = "error"
DONE: Final = "done"
FAILED: Final = "failed"
ENFORCEMENT: Final = "enforcement"


def send_enforcement(conn: Any, applied: bool, reason: str | None) -> None:
    """Report the outcome of this call's OS-level capability enforcement
    attempt (`sandbox/enforcement.py`) — sent once, unconditionally, right
    after attempting it and before the plugin's own method runs, so the
    parent can warn if a registration that predicted real enforcement
    (`enforcement_available()` at register() time) didn't actually get it
    for this specific call (security review finding: enforcement can fail
    at call time for reasons registration time can't predict, e.g. a
    container's own outer seccomp profile)."""
    conn.send((ENFORCEMENT, applied, reason))


def send_call(
    conn: Any, tag: str, method_name: str, args: tuple[object, ...], kwargs: dict[str, object]
) -> None:
    """Send a CALL message: `tag`'s remote object should run `method_name`."""
    conn.send((CALL, tag, method_name, args, kwargs))


def send_result(conn: Any, value: object) -> None:
    """Reply to the CALL currently awaiting a reply with a successful `value`."""
    conn.send((RESULT, value))


def send_error(conn: Any, wired_exception: object) -> None:
    """Reply to the CALL currently awaiting a reply with `wired_exception`."""
    conn.send((ERROR, wired_exception))


def send_done(conn: Any, wired_result: object) -> None:
    """End the exchange: the plugin's own method returned `wired_result`."""
    conn.send((DONE, wired_result))


def send_failed(conn: Any, wired_exception: object) -> None:
    """End the exchange: the plugin's own method raised `wired_exception`."""
    conn.send((FAILED, wired_exception))


def recv_from_child(conn: Any) -> tuple[Any, ...]:
    """Read one message from the child's side of the pipe, safely.

    The child process runs the plugin's own, untrusted code — it need not
    even use this module's `send_*` helpers to reach `conn`; it has the
    raw file descriptor and can write a hand-crafted, hostile pickle
    stream directly. `wire.restricted_loads` is what actually contains
    that: it refuses to construct any class instance not on its explicit
    exception allowlist, closing the arbitrary-code-execution surface
    plain `pickle.loads()`/`Connection.recv()` would otherwise hand a
    malicious plugin (a `REDUCE` opcode can call any importable callable
    with attacker-chosen arguments the instant `.loads()` runs).

    Raises:
        EOFError: the child's end of the pipe closed with nothing to read
            (it crashed, was killed, or exited without a final message).
        pickle.UnpicklingError: the bytes contained a disallowed class
            reference — treated by the dispatch loop as a protocol
            violation, not silently accepted.
    """
    data = conn.recv_bytes()
    message = restricted_loads(data)
    if not isinstance(message, tuple) or not message:
        raise pickle.UnpicklingError(f"Malformed sandbox protocol message: {message!r}")
    return message


__all__ = [
    "CALL",
    "RESULT",
    "ERROR",
    "DONE",
    "FAILED",
    "ENFORCEMENT",
    "send_call",
    "send_result",
    "send_error",
    "send_done",
    "send_failed",
    "send_enforcement",
    "recv_from_child",
]
