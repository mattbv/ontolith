"""Unit tests for the MCP server (ADR-0008).

Verifies that all 6 tools return the correct structure and that the no-write
invariant holds. Tests run against a real in-memory SQLite KB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.interfaces.mcp import create_mcp_server

T0 = datetime(2025, 1, 1, tzinfo=UTC)
HUMAN = "alice@example.com"
AI = "scout-agent"
AI_OWNER = HUMAN
REVIEWER = "bob@example.com"


def _kb(tmp_path: Path) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(50)])
    kb = Ontology.connect(tmp_path / "test.db", clock=clock, id_provider=ids)
    kb.create_principal(HUMAN, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(REVIEWER, kind="human", auth_method="oidc", default_capability="review")
    kb.create_principal(
        AI, kind="ai", auth_method="apikey", owner=AI_OWNER, default_capability="propose"
    )
    return kb


# ---------------------------------------------------------------------------
# ontolith.get
# ---------------------------------------------------------------------------


class TestGetTool:
    def test_get_known_entity(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.get").fn(entity_id=entity.id)

        assert result["entity"]["id"] == entity.id
        assert result["entity"]["concept"] == "Person"
        assert len(result["assertions"]) == 1
        assert result["assertions"][0]["value"] == "Ada"

    def test_get_unknown_entity_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.get").fn(entity_id="does-not-exist")
        assert "error" in result

    def test_get_excludes_non_active_assertions(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        kb.retract(active[0].id, HUMAN)

        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.get").fn(entity_id=entity.id)
        assert result["assertions"] == []


# ---------------------------------------------------------------------------
# ontolith.query
# ---------------------------------------------------------------------------


class TestQueryTool:
    def test_query_all_of_concept(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)
        kb.create_entity("Person", author=HUMAN)
        kb.create_entity("Organization", author=HUMAN)

        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(concept="Person")
        assert result["count"] == 2
        assert len(result["entities"]) == 2

    def test_query_with_filter(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        e1 = kb.create_entity("Person", author=HUMAN)
        e2 = kb.create_entity("Person", author=HUMAN)
        kb.propose(e1.id, "Person.name", "Ada", "Text", HUMAN)
        kb.propose(e2.id, "Person.name", "Grace", "Text", HUMAN)

        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person", filters={"name": "Ada"}
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == e1.id

    def test_query_empty_concept_returns_empty(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(concept="Organization")
        assert result["count"] == 0
        assert result["entities"] == []


# ---------------------------------------------------------------------------
# ontolith.provenance
# ---------------------------------------------------------------------------


class TestProvenanceTool:
    def test_provenance_known_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN, confidence=0.95, source="wiki")
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(assertion_id=assertions[0].id)

        assert result["id"] == assertions[0].id
        assert result["author"] == HUMAN
        assert result["confidence"] == 0.95
        assert result["source"] == "wiki"
        assert result["subject"] == entity.id

    def test_provenance_unknown_assertion_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(assertion_id="nonexistent")
        assert "error" in result


# ---------------------------------------------------------------------------
# ontolith.propose
# ---------------------------------------------------------------------------


class TestProposeTool:
    def test_propose_human_auto_accepted(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            author=HUMAN,
        )

        assert result["proposal"]["state"] == "auto_accepted"
        assert result["decision"] == "AutoAccept"

    def test_propose_ai_requires_review(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            author=AI,
        )

        assert result["proposal"]["state"] == "require_review"
        assert result["decision"] == "RequireReview"

    def test_propose_unknown_author_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            author="nobody@example.com",
        )
        assert "error" in result
        assert result["code"] == "auth_error"

    def test_propose_does_not_expose_direct_write(self, tmp_path: Path) -> None:
        """MCP propose tool must not bypass policy — AI assertions need review."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp = create_mcp_server(kb)
        mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            author=AI,
        )

        # No assertion written yet — still pending review
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active == []

    def test_no_write_tool_registered(self, tmp_path: Path) -> None:
        """ADR-0008: no direct write, update, or delete tool must be registered."""
        kb = _kb(tmp_path)
        mcp = create_mcp_server(kb)
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        forbidden = {"ontolith.write", "ontolith.update", "ontolith.delete", "ontolith.assert"}
        assert tool_names.isdisjoint(forbidden)

    def test_all_required_tools_registered(self, tmp_path: Path) -> None:
        """ADR-0008: all 6 required tools must be present."""
        kb = _kb(tmp_path)
        mcp = create_mcp_server(kb)
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        required = {
            "ontolith.schema",
            "ontolith.get",
            "ontolith.query",
            "ontolith.provenance",
            "ontolith.propose",
            "ontolith.flag_contradiction",
        }
        assert required.issubset(tool_names)


# ---------------------------------------------------------------------------
# ontolith.flag_contradiction
# ---------------------------------------------------------------------------


class TestFlagContradictionTool:
    def test_flag_creates_contradiction(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        # Need a second assertion — insert directly via backend
        from ontolith.core import Assertion

        a2 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada Lovelace",
            author=HUMAN,
            asserted_at=T0,
            status="active",
        )
        kb.backend.put_assertion(a2)

        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=assertions[0].id,
            assertion_id_b=a2.id,
            author=HUMAN,
        )

        assert "contradiction_id" in result
        assert result["action"] == "created"
        assert set(result["member_ids"]) == {assertions[0].id, a2.id}

    def test_flag_different_predicates_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        kb.propose(entity.id, "Person.born", "1815-12-10", "Date", HUMAN)

        all_a = kb.assertions(subject=entity.id, status="active")
        name_a = next(a for a in all_a if a.predicate == "Person.name")
        born_a = next(a for a in all_a if a.predicate == "Person.born")

        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=name_a.id,
            assertion_id_b=born_a.id,
            author=HUMAN,
        )
        assert "error" in result

    def test_flag_unknown_author_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp = create_mcp_server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a="a1",
            assertion_id_b="a2",
            author="nobody@example.com",
        )
        assert "error" in result
        assert result["code"] == "auth_error"
