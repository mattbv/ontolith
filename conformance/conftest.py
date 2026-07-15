"""Shared fixtures for the backend conformance kit (SPEC §19).

Every conformance test that touches a StorageBackend is parametrized, via
the `make_kb` fixture, to run once per registered backend — this is the
mechanism proving the StorageBackend port abstraction for the M3 exit
criterion ("Backend conformance kit passes on a 2nd backend", ADR-0016).

Tests that need to poke at backend-internal implementation details (e.g.
raw DB constraint enforcement, exception types) construct a concrete
backend directly instead of using this fixture — see
test_accountable_owner.py's test_ai_owner_*_enforced_at_*_layer tests.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import pytest

from ontolith import Ontology
from ontolith.core import Clock, IdProvider
from ontolith.govern.policy import PolicyStrategy
from ontolith.store.base import StorageBackend
from ontolith.store.duckdb import DuckDBBackend
from ontolith.store.sqlite import SQLiteBackend


def _sqlite_factory(path: Path, clock: Clock | None) -> StorageBackend:
    return SQLiteBackend(path, clock=clock)


def _duckdb_factory(path: Path, clock: Clock | None) -> StorageBackend:
    return DuckDBBackend(path, clock=clock)


_BACKEND_FACTORIES: dict[str, Callable[[Path, Clock | None], StorageBackend]] = {
    "sqlite": _sqlite_factory,
    "duckdb": _duckdb_factory,
}


class KbFactory(Protocol):
    """Factory for a fresh Ontology over the current parametrized backend.

    `last_path` exposes the path of the most recently created database —
    StorageBackend itself declares no `path` attribute (a future
    non-file-based backend wouldn't have one), so reconnect-durability
    tests go through this instead of reaching into `kb.backend.path`.
    """

    last_path: Path

    def __call__(
        self,
        clock: Clock | None = None,
        id_provider: IdProvider | None = None,
        *,
        reuse_path: Path | None = None,
        policy: PolicyStrategy | None = None,
    ) -> Ontology: ...


@pytest.fixture(params=sorted(_BACKEND_FACTORIES))
def backend_name(request: pytest.FixtureRequest) -> str:
    """Parametrized backend identifier ('sqlite' or 'duckdb')."""
    return str(request.param)


class _KbFactoryImpl:
    def __init__(
        self, tmp_path: Path, factory: Callable[[Path, Clock | None], StorageBackend]
    ) -> None:
        self._tmp_path = tmp_path
        self._factory = factory
        self._counter = 0
        self.last_path: Path = tmp_path

    def __call__(
        self,
        clock: Clock | None = None,
        id_provider: IdProvider | None = None,
        *,
        reuse_path: Path | None = None,
        policy: PolicyStrategy | None = None,
    ) -> Ontology:
        self._counter += 1
        path = reuse_path or (self._tmp_path / f"kb-{self._counter}.db")
        self.last_path = path
        backend = self._factory(path, clock)
        return Ontology(backend, clock=clock, id_provider=id_provider, policy=policy)


@pytest.fixture
def make_kb(tmp_path: Path, backend_name: str) -> KbFactory:
    """Replaces ad hoc `Ontology.connect(path, clock=..., id_provider=...)`
    construction in conformance tests, parametrized transparently over
    every registered backend.

    Pass `reuse_path=` to reconnect to a previously-created database file
    (durability-across-connections tests); `.last_path` gives the path of
    the most recently created database for that purpose.
    """
    return _KbFactoryImpl(tmp_path, _BACKEND_FACTORIES[backend_name])
