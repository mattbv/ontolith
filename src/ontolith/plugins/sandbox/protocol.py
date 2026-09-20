"""Wire message shapes for the plugin sandbox's single duplex pipe (ADR-0051).

One `multiprocessing.Pipe()` carries every message for one isolated plugin
call. Messages are plain tuples (`multiprocessing.connection.Connection`
pickles whatever is sent) tagged by a leading string so both ends can
dispatch on it. There is never more than one request in flight at a time —
the child blocks on `recv()` after every `CALL` until it gets a matching
`RESULT`/`ERROR`, so no request/response correlation id is needed beyond the
per-argument `tag` a `CALL` message already carries (which argument's proxy
made this call), not a call-sequence id.

Child -> parent, during the plugin's own method execution (any number of
times, always answered before the child sends anything else):
    (CALL, tag, method_name, args, kwargs)

Parent -> child, one per CALL, always the very next message on the pipe:
    (RESULT, value)
    (ERROR, wired_exception)

Child -> parent, exactly once, ending the exchange:
    (DONE, wired_result)
    (FAILED, wired_exception)
"""

from __future__ import annotations

from typing import Any, Final

CALL: Final = "call"
RESULT: Final = "result"
ERROR: Final = "error"
DONE: Final = "done"
FAILED: Final = "failed"


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


__all__ = [
    "CALL",
    "RESULT",
    "ERROR",
    "DONE",
    "FAILED",
    "send_call",
    "send_result",
    "send_error",
    "send_done",
    "send_failed",
]
