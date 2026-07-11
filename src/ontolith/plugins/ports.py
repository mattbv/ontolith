"""Plugin protocol interfaces (SPEC §13.2, ADR-0015).

Each protocol receives a capability-scoped view (ReadOnlyView or WriteView,
plugins/views.py) rather than the raw Ontology/StorageBackend — this is what
gives PluginRegistry's capability negotiation teeth. Method shapes here are
skeletons for future reference-plugin implementations (KI-010); they are not
yet exercised by any concrete plugin.

Deliberately excludes StorageBackend, AuthProvider, and PolicyStrategy —
those are infrastructure-extension ports the framework calls INTO, not
principal-scoped actors that call INTO the framework via a view (see
manifest.py's PluginKind docstring and ADR-0015 "Plugin kinds in scope").
AuthProvider and PolicyStrategy already have real implementations elsewhere
(identity/ports.py, govern/policy.py); StorageBackend's port lives in
store/base.py. Embedder has no real port yet anywhere in the codebase — M1-M2
never delivered it, and defining it isn't this pass's job either.
"""

from typing import Protocol

from ontolith.core import Assertion
from ontolith.plugins.views import ReadOnlyView, WriteView


class Importer(Protocol):
    """Imports external data into the KB through a governed WriteView."""

    def import_(self, source: object, kb: WriteView) -> object: ...


class Exporter(Protocol):
    """Exports KB data to an external target through a ReadOnlyView."""

    def export(self, kb: ReadOnlyView, target: object) -> object: ...


class Reasoner(Protocol):
    """Derives new assertions from existing KB state through a WriteView.

    Derived assertions MUST be submitted via kb.propose()/kb.propose_ref() —
    WriteView has no other write path. This is what SPEC §13.2's "reasoner-
    derived assertions MUST enter through the proposal path" means in
    practice: there is no bypass to forget to block.
    """

    def derive(self, kb: WriteView) -> None: ...


class Validator(Protocol):
    """Validates an assertion against custom rules through a ReadOnlyView."""

    def validate(self, assertion: Assertion, kb: ReadOnlyView) -> list[str]: ...


class Connector(Protocol):
    """Syncs KB state with an external system through a governed WriteView."""

    def sync(self, kb: WriteView) -> object: ...


__all__ = ["Importer", "Exporter", "Reasoner", "Validator", "Connector"]
