"""Unit tests for ReadOnlyView/WriteView (ADR-0015).

Table-driven absence checks are the core assertion here: admin-only and
direct-write methods must be structurally absent, not merely unreachable at
runtime. This is what makes the isolation model correctness-checkable by
reading the class definition rather than testing every call site.
"""

import tempfile
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.plugins.views import ReadOnlyView, WriteView

_FORBIDDEN_METHODS = [
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
    "require_admin",
]


@pytest.fixture
def kb() -> Ontology:
    with tempfile.TemporaryDirectory() as tmpdir:
        kb = Ontology.connect(Path(tmpdir) / "test.db")
        kb.create_principal("plugin-x", kind="service", auth_method="workload")
        yield kb
        kb.close()


class TestReadOnlyView:
    @pytest.mark.parametrize("method_name", _FORBIDDEN_METHODS)
    def test_forbidden_method_absent(self, kb: Ontology, method_name: str) -> None:
        view = ReadOnlyView(kb, "plugin-x")
        assert not hasattr(view, method_name)

    def test_no_write_methods_present(self, kb: Ontology) -> None:
        view = ReadOnlyView(kb, "plugin-x")
        for method_name in ("propose", "propose_ref", "retract", "create_entity"):
            assert not hasattr(view, method_name)

    def test_get_entity(self, kb: Ontology) -> None:
        entity = kb.create_entity("Person", author="plugin-x")
        view = ReadOnlyView(kb, "plugin-x")
        assert view.get_entity(entity.id) is not None

    def test_assertions(self, kb: Ontology) -> None:
        entity = kb.create_entity("Person", author="plugin-x")
        view = ReadOnlyView(kb, "plugin-x")
        assert view.assertions(subject=entity.id) == []

    def test_query(self, kb: Ontology) -> None:
        view = ReadOnlyView(kb, "plugin-x")
        assert view.query("Person").all() == []

    def test_as_of(self, kb: Ontology) -> None:
        view = ReadOnlyView(kb, "plugin-x")
        as_of_view = view.as_of(kb.clock.now())
        assert as_of_view.assertions() == []

    def test_principal_id_property(self, kb: Ontology) -> None:
        view = ReadOnlyView(kb, "plugin-x")
        assert view.principal_id == "plugin-x"


class TestWriteView:
    @pytest.mark.parametrize("method_name", _FORBIDDEN_METHODS)
    def test_forbidden_method_absent(self, kb: Ontology, method_name: str) -> None:
        view = WriteView(kb, "plugin-x")
        assert not hasattr(view, method_name)

    def test_no_acting_as_parameter_on_propose(self, kb: Ontology) -> None:
        """No view method accepts acting_as — plugins can never act as
        another principal (ADR-0015: reopens the ADR-0014 spoofing class)."""
        import inspect

        view = WriteView(kb, "plugin-x")
        sig = inspect.signature(view.propose)
        assert "acting_as" not in sig.parameters
        sig = inspect.signature(view.propose_ref)
        assert "acting_as" not in sig.parameters
        sig = inspect.signature(view.retract)
        assert "acting_as" not in sig.parameters

    def test_create_entity_authored_by_bound_principal(self, kb: Ontology) -> None:
        view = WriteView(kb, "plugin-x")
        entity = view.create_entity("Person")
        assert entity.created_by == "plugin-x"

    def test_propose_authored_by_bound_principal(self, kb: Ontology) -> None:
        entity = kb.create_entity("Person", author="plugin-x")
        view = WriteView(kb, "plugin-x")

        proposal, decision = view.propose(entity.id, "Person.name", "Ada", "Text")
        assert proposal.author == "plugin-x"

    def test_propose_ref_authored_by_bound_principal(self, kb: Ontology) -> None:
        person = kb.create_entity("Person", author="plugin-x")
        org = kb.create_entity("Organization", author="plugin-x")
        view = WriteView(kb, "plugin-x")

        proposal, decision = view.propose_ref(person.id, "Person.employer", org.id)
        assert proposal.author == "plugin-x"

    def test_retract_authored_by_bound_principal(self, kb: Ontology) -> None:
        kb.create_principal("writer@example.com", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author="writer@example.com")
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "writer@example.com")

        view = WriteView(kb, "plugin-x")
        proposal, decision = view.retract(assertion.id)
        assert proposal.author == "plugin-x"
