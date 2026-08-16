"""Plugin protocol interfaces (SPEC §13.2, ADR-0015).

Each protocol receives a capability-scoped view (ReadOnlyView or WriteView,
plugins/views.py) rather than the raw Ontology/StorageBackend — this is what
gives PluginRegistry's capability negotiation teeth. Method shapes here are
skeletons for future reference-plugin implementations (KI-010); they are not
yet exercised by any concrete plugin.

Validator is the one exception: its `kb` parameter is typed as the
structural `ValidatorKbView` Protocol, not the concrete `ReadOnlyView`
(KI-042, ADR-0029). A Validator loaded through `PluginRegistry.register()`
still only ever receives a real, capability-scoped `ReadOnlyView` — but a
Validator registered directly on `Ontology` (its `validators`/
`completeness_validators` constructor parameters) receives the live
`Ontology` instance itself, trusted the same way `PolicyStrategy` already
is (ADR-0018), not sandboxed via a view. `ValidatorKbView` is the minimal
shape both satisfy.

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

from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from ontolith.core import Assertion, Entity
from ontolith.plugins.views import ReadOnlyView, WriteView
from ontolith.query import QueryBuilder

if TYPE_CHECKING:
    from ontolith.ontology import AsOfView


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


class ValidatorKbView(Protocol):
    """Structural read view a Validator may query (KI-042, ADR-0029).

    Deliberately narrower than the concrete `ReadOnlyView` class this
    protocol is otherwise modeled on: `Ontology` itself (`ontology.py`)
    already exposes this exact method shape and is what gets passed
    directly as `kb` when a Validator registered via `Ontology`'s own
    `validators`/`completeness_validators` constructor parameters runs
    synchronously inside the write path (as opposed to a Validator loaded
    through `PluginRegistry.register()`, which still receives a real,
    capability-scoped `ReadOnlyView`). Importing `Ontology` here to spell
    that out as a second concrete type would be circular — `ontology.py`
    needs `Validator`'s type for its own constructor parameters, and this
    module already imports `ReadOnlyView`/`WriteView` from
    `plugins/views.py`, which itself imports `Ontology`. This minimal
    Protocol is the common shape both `ReadOnlyView` and `Ontology`
    already satisfy structurally, with no inheritance or import required —
    the same pattern `govern/policy.py`'s `KbView` uses for the same
    reason (KI-017, ADR-0025).
    """

    def get_entity(self, entity_id: str) -> Entity | None:
        """Retrieve an entity by ID."""
        ...

    def assertions(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        status: str | None = "active",
    ) -> list[Assertion]:
        """Query assertions."""
        ...

    def query(self, concept: str) -> QueryBuilder:
        """Create a query builder for a concept."""
        ...

    def as_of(self, t: datetime | str) -> "AsOfView":
        """Return a read-only bitemporal view at time t (SPEC §11.4)."""
        ...


class Validator(Protocol):
    """Validates an assertion against custom rules through a read-only KB view.

    `assertion` is the thing under validation when a Validator is invoked
    per-assertion (e.g. via `Ontology.validators` — KI-042, ADR-0029). A
    Validator invoked for whole-entity completeness instead (e.g. via
    `Ontology.completeness_validators`) receives an `assertion` that is
    only a *subject* stand-in — implementations with that shape (like
    `RequiredFieldsValidator`) should read `assertion.subject` and re-query
    `kb` for current state, and must not rely on `assertion`'s other
    fields describing anything currently true or just-committed.
    """

    def validate(self, assertion: Assertion, kb: ValidatorKbView) -> list[str]:
        """Return a list of validation error messages, empty if `assertion` is valid."""
        ...


class Connector(Protocol):
    """Syncs KB state with an external system through a governed WriteView."""

    def sync(self, kb: WriteView) -> object:
        """Reconcile the external system's state with the KB via `kb`."""
        ...


__all__ = ["Importer", "Exporter", "Reasoner", "Validator", "ValidatorKbView", "Connector"]
