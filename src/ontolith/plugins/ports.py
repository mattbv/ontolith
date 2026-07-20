"""Plugin protocol interfaces (SPEC §13.2, ADR-0015).

Each protocol receives a capability-scoped view (ReadOnlyView or WriteView,
plugins/views.py) rather than the raw Ontology/StorageBackend — this is what
gives PluginRegistry's capability negotiation teeth. Method shapes here are
skeletons for future reference-plugin implementations (KI-010); they are not
yet exercised by any concrete plugin.

Deliberately excludes StorageBackend, AuthProvider, PolicyStrategy, and
Embedder — those are infrastructure-extension ports the framework calls
INTO, not principal-scoped actors that call INTO the framework via a view
(see manifest.py's PluginKind docstring and ADR-0015 "Plugin kinds in
scope"). AuthProvider and PolicyStrategy already have real implementations
elsewhere (identity/ports.py, govern/policy.py); StorageBackend's port lives
in store/base.py; Embedder's port lives in core/embedder.py (ADR-0020) —
its Protocol takes no `kb` parameter, so it can't fit this file's
view-based pattern the way Importer/Exporter/Reasoner/Validator/Connector
do.
"""

from typing import Protocol

from ontolith.core import Assertion
from ontolith.plugins.views import ReadOnlyView, WriteView


class Importer(Protocol):
    """Imports external data into the KB through a governed WriteView."""

    def import_(self, source: object, kb: WriteView) -> object:
        """Read `source` and write the resulting entities/assertions via `kb`."""
        ...


class Exporter(Protocol):
    """Exports KB data to an external target through a ReadOnlyView."""

    def export(self, kb: ReadOnlyView, target: object) -> object:
        """Read KB state via `kb` and write it out to `target`."""
        ...


class Reasoner(Protocol):
    """Derives new assertions from existing KB state through a WriteView.

    Derived assertions MUST be submitted via kb.propose()/kb.propose_ref() —
    WriteView has no other write path. This is what SPEC §13.2's "reasoner-
    derived assertions MUST enter through the proposal path" means in
    practice: there is no bypass to forget to block.
    """

    def derive(self, kb: WriteView) -> None:
        """Inspect KB state via `kb` and propose any derived assertions."""
        ...


class Validator(Protocol):
    """Validates an assertion against custom rules through a ReadOnlyView."""

    def validate(self, assertion: Assertion, kb: ReadOnlyView) -> list[str]:
        """Return a list of validation error messages, empty if `assertion` is valid."""
        ...


class Connector(Protocol):
    """Syncs KB state with an external system through a governed WriteView."""

    def sync(self, kb: WriteView) -> object:
        """Reconcile the external system's state with the KB via `kb`."""
        ...


__all__ = ["Importer", "Exporter", "Reasoner", "Validator", "Connector"]
