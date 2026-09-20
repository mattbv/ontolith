"""Child-side proxies for a plugin's `kb` view and any writable target
(ADR-0051).

`RemoteReadOnlyView`/`RemoteWriteView` mirror `plugins/views.py`'s public
method set and subclass relationship exactly, so a plugin's own
`isinstance(kb, WriteView)` check (none of the shipped reference plugins do
this, but nothing stops a third-party one from it) still holds inside the
sandboxed child — but every method sends one `protocol.CALL` message over
the shared pipe and blocks for the matching `RESULT`/`ERROR` instead of
touching a real `Ontology`/backend, which only the parent process ever
holds a reference to.

`.query()`/`.as_of()` are deliberately unsupported here — both return a
fluent builder holding a live backend reference, which would need its own
remote-proxy protocol this pass doesn't build (no shipped reference plugin
calls either method; see ADR-0051's Consequences and KI-101).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ontolith.core import Assertion, Entity
from ontolith.core.errors import PluginError
from ontolith.govern.policy import Decision
from ontolith.govern.proposal import Proposal
from ontolith.plugins.sandbox import protocol
from ontolith.plugins.sandbox.wire import unwire_exception
from ontolith.query import QueryBuilder
from ontolith.schema import SchemaIR

if TYPE_CHECKING:
    from ontolith.ontology import AsOfView

_QUERY_NOT_SUPPORTED = (
    "kb.{method}(...) is not available from inside a sandboxed plugin call (ADR-0051) — "
    "it returns a fluent builder holding a live backend reference, which has no remote-proxy "
    "protocol in this pass. Use kb.assertions(...) instead, or register this plugin with "
    "isolate=False if it genuinely needs {method}()."
)


class _RemoteCallMixin:
    """Shared request/reply plumbing for every remote view proxy method."""

    def __init__(self, conn: Any, tag: str) -> None:
        self._conn = conn
        self._tag = tag

    def _call(self, method_name: str, *args: object, **kwargs: object) -> Any:
        """Send one CALL for `method_name(*args, **kwargs)` and block for
        the matching RESULT/ERROR, raising the unwired exception on ERROR."""
        protocol.send_call(self._conn, self._tag, method_name, args, kwargs)
        kind, payload = self._conn.recv()
        if kind == protocol.ERROR:
            raise unwire_exception(payload)
        return payload


class RemoteReadOnlyView(_RemoteCallMixin):
    """Sandboxed-child proxy for `plugins.views.ReadOnlyView`."""

    def get_entity(self, entity_id: str) -> Entity | None:
        """Retrieve an entity by ID, via the parent's real view."""
        return self._call("get_entity", entity_id)  # type: ignore[no-any-return]

    def schema(self) -> SchemaIR | None:
        """The current schema for this view's namespace, via the parent's real view."""
        return self._call("schema")  # type: ignore[no-any-return]

    def assertions(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        status: str | None = "active",
    ) -> list[Assertion]:
        """Query assertions, via the parent's real view."""
        return self._call(  # type: ignore[no-any-return]
            "assertions", subject=subject, predicate=predicate, status=status
        )

    def query(self, concept: str) -> QueryBuilder:
        """Not supported inside a sandboxed plugin call — see module docstring."""
        raise PluginError(_QUERY_NOT_SUPPORTED.format(method="query"))

    def as_of(self, t: datetime | str) -> AsOfView:
        """Not supported inside a sandboxed plugin call — see module docstring."""
        raise PluginError(_QUERY_NOT_SUPPORTED.format(method="as_of"))


class RemoteWriteView(RemoteReadOnlyView):
    """Sandboxed-child proxy for `plugins.views.WriteView`."""

    def create_entity(self, concept: str, natural_key: str | None = None) -> Entity:
        """Create a new entity, via the parent's real view."""
        return self._call("create_entity", concept, natural_key=natural_key)  # type: ignore[no-any-return]

    def propose(
        self,
        subject: str,
        predicate: str,
        value: str,
        value_type: str,
        *,
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
    ) -> tuple[Proposal, Decision]:
        """Submit a literal assertion, via the parent's real view."""
        return self._call(  # type: ignore[no-any-return]
            "propose",
            subject,
            predicate,
            value,
            value_type,
            confidence=confidence,
            source=source,
            rationale=rationale,
        )

    def propose_ref(
        self,
        subject: str,
        predicate: str,
        target: str,
        *,
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
    ) -> tuple[Proposal, Decision]:
        """Submit a reference assertion, via the parent's real view."""
        return self._call(  # type: ignore[no-any-return]
            "propose_ref",
            subject,
            predicate,
            target,
            confidence=confidence,
            source=source,
            rationale=rationale,
        )

    def retract(self, assertion_id: str) -> tuple[Proposal, Decision]:
        """Propose retraction of an assertion, via the parent's real view."""
        return self._call("retract", assertion_id)  # type: ignore[no-any-return]


class RemoteWritable(_RemoteCallMixin):
    """Sandboxed-child proxy for a non-picklable `.write`-shaped argument
    (e.g. an `io.StringIO` passed as an exporter's `target`)."""

    def write(self, data: object) -> object:
        """Write `data` through to the parent's real object."""
        return self._call("write", data)


__all__ = ["RemoteReadOnlyView", "RemoteWriteView", "RemoteWritable"]
