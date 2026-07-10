"""Conformance-style capstone for plugin capability isolation (ADR-0015).

Loads a plugin through the full PluginRegistry.register() path and confirms
the intended API surface has no route to admin-only or direct-write methods.

Scope note (see ADR-0015 Consequences): this proves the accidental/structural
bypass is closed for a plugin using its view normally. It does NOT and
cannot prove a deliberately malicious plugin can't reach past the view's
private `_kb` reference — Python has no true encapsulation, so
`loaded.view._kb.issue_token(...)` remains reachable to code that goes
looking for it. That gap requires process/wasm isolation, explicitly
deferred future work, not a testable property of this module.
"""

import tempfile
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.plugins.registry import PluginRegistry
from ontolith.plugins.views import WriteView

ADMIN = "admin@example.com"

_ADMIN_ONLY_AND_DIRECT_WRITE_METHODS = [
    "issue_token",
    "revoke_token",
    "list_tokens",
    "apply_schema",
    "create_principal",
    "accept_proposal",
    "reject_proposal",
    "resolve_contradiction",
    "flag_contradiction",
    "assert_literal",
    "assert_ref",
]


@pytest.fixture
def kb() -> Ontology:
    with tempfile.TemporaryDirectory() as tmpdir:
        kb = Ontology.connect(Path(tmpdir) / "test.db")
        kb.create_principal(ADMIN, kind="human", default_capability="admin")
        yield kb
        kb.close()


def test_registered_plugin_has_no_route_to_admin_or_direct_write_methods(
    kb: Ontology, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_entry_points(*, group: str) -> list[EntryPoint]:
        assert group == "ontolith.plugins"
        return [
            EntryPoint(
                name="trivial-importer",
                value="tests.fixtures.plugins.trivial_importer:TrivialImporter",
                group="ontolith.plugins",
            )
        ]

    monkeypatch.setattr("ontolith.plugins.registry.entry_points", fake_entry_points)

    registry = PluginRegistry(kb)
    loaded = registry.register("trivial-importer", author=ADMIN, granted_capability="write")

    assert isinstance(loaded.view, WriteView)
    for method_name in _ADMIN_ONLY_AND_DIRECT_WRITE_METHODS:
        assert getattr(loaded.view, method_name, None) is None, (
            f"{method_name} must not be reachable from a plugin's view"
        )


def test_registered_plugin_can_only_write_through_governed_propose(
    kb: Ontology, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a plugin's write lands as a properly governed, attributed
    assertion — not a raw backend.put_assertion bypass."""

    def fake_entry_points(*, group: str) -> list[EntryPoint]:
        return [
            EntryPoint(
                name="trivial-importer",
                value="tests.fixtures.plugins.trivial_importer:TrivialImporter",
                group="ontolith.plugins",
            )
        ]

    monkeypatch.setattr("ontolith.plugins.registry.entry_points", fake_entry_points)

    registry = PluginRegistry(kb)
    loaded = registry.register("trivial-importer", author=ADMIN, granted_capability="write")
    assert isinstance(loaded.view, WriteView)

    entity = loaded.view.create_entity("Person")
    proposal, decision = loaded.view.propose(entity.id, "Person.name", "Ada", "Text")

    assert proposal.author == loaded.principal_id == "trivial-importer"
    active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
    assert len(active) == 1
    assert active[0].author == "trivial-importer"
