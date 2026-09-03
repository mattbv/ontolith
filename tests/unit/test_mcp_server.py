"""Unit tests for the MCP server (ADR-0008, ADR-0014).

Verifies that all 8 tools return the correct structure, that the no-write
invariant holds, and that the acting principal is always resolved from a
verified bearer token (ADR-0014), never a caller-supplied ID. Tests run
against a real in-memory SQLite KB.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from starlette.datastructures import Headers

from ontolith import Ontology
from ontolith.core import Assertion, FixedClock, FixedIdProvider
from ontolith.identity.token_auth import TokenAuthProvider
from ontolith.interfaces.mcp import create_mcp_server
from ontolith.schema.ir import ConceptDef, PropertyDef, RelationDef, SchemaIR

T0 = datetime(2025, 1, 1, tzinfo=UTC)
HUMAN = "alice@example.com"
AI = "scout-agent"
AI_OWNER = HUMAN
REVIEWER = "bob@example.com"
ADMIN = "admin@example.com"


def _kb(tmp_path: Path) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(50)])
    kb = Ontology.connect(tmp_path / "test.db", clock=clock, id_provider=ids)
    kb.create_principal(HUMAN, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(REVIEWER, kind="human", auth_method="oidc", default_capability="review")
    kb.create_principal(
        AI, kind="ai", auth_method="apikey", owner=AI_OWNER, default_capability="propose"
    )
    kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
    return kb


def _server(kb: Ontology) -> tuple:
    auth_provider = TokenAuthProvider(kb.backend)
    return create_mcp_server(kb, auth_provider), auth_provider


@contextlib.contextmanager
def _http_request(authorization: str | None) -> Iterator[None]:
    """Simulate a tool call arriving over the SSE/streamable-HTTP transport
    with the given raw `Authorization` header value (KI-067) — mirrors the
    mcp SDK's own `ServerMessageMetadata(request_context=<starlette Request>)`
    wiring, which `create_mcp_server`'s `_bearer_token` helper reads via
    `mcp.get_context().request_context.request`. `authorization=None` means
    a live HTTP request with no such header at all — distinct from no HTTP
    request/context existing (the plain `.fn(...)` call outside this context
    manager, which every pre-KI-067 test in this file already exercises)."""
    headers = Headers({"Authorization": authorization} if authorization is not None else {})
    fake_request = SimpleNamespace(headers=headers)
    ctx = RequestContext(
        request_id="test-request",
        meta=None,
        session=None,
        lifespan_context=None,
        request=fake_request,
    )
    reset_token = request_ctx.set(ctx)
    try:
        yield
    finally:
        request_ctx.reset(reset_token)


async def _call_tool_over_real_transport(
    mcp: object, tool_name: str, arguments: dict[str, object], authorization: str | None
) -> object:
    """Drive one tool call through the real streamable-HTTP ASGI transport —
    a genuine JSON-RPC initialize handshake, real session management, and an
    actual `Authorization` header on an in-process HTTP request — instead of
    `_http_request`'s faked `RequestContext`. Closes the gap where the fake
    could silently drift from the SDK's real wiring while every faked-context
    test kept passing (KI-067 review). Uses `httpx` (not this repo's `httpx2`
    used elsewhere in this file) because `mcp.client.streamable_http` is
    itself built on plain `httpx`, not `httpx2`.
    """
    import httpx
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    app = mcp.streamable_http_app()  # type: ignore[attr-defined]

    headers = {"Authorization": authorization} if authorization is not None else {}
    # DNS-rebinding protection (mcp SDK default) rejects a bare "localhost"
    # Host header with no port — base_url must include one.
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000", headers=headers
    )

    async with mcp.session_manager.run():  # type: ignore[attr-defined]
        async with http_client:
            async with streamable_http_client(
                "http://localhost:8000/mcp", http_client=http_client
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    return result.structuredContent


# ---------------------------------------------------------------------------
# ontolith.schema
# ---------------------------------------------------------------------------


class TestSchemaTool:
    def test_schema_returns_empty_when_no_schema_stored(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.schema").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result == {"concepts": []}

    def test_schema_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.schema").fn(token="not-a-real-token")
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_schema_returns_concepts_and_properties(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text", required=True),
                        "employer": PropertyDef(
                            name="employer", value_type="Text", temporality="time_varying"
                        ),
                        "nicknames": PropertyDef(
                            name="nicknames", value_type="Text", cardinality="many"
                        ),
                    },
                ),
            },
        )
        kb.backend.put_schema(schema)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.schema").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["namespace"] == "default"
        assert result["version"] == 1
        assert len(result["concepts"]) == 1
        person = result["concepts"][0]
        assert person["name"] == "Person"
        props_by_name = {p["name"]: p for p in person["properties"]}
        assert props_by_name["name"]["type"] == "Text"
        assert props_by_name["name"]["required"] is True
        assert props_by_name["name"]["cardinality"] == "single"
        assert props_by_name["employer"]["temporality"] == "time_varying"
        assert props_by_name["nicknames"]["cardinality"] == "many"
        assert person["relations"] == []

    def test_schema_returns_relations(self, tmp_path: Path) -> None:
        """KI-029: relations were previously omitted entirely - an agent
        inspecting the schema this way couldn't see that Person.employer
        exists, or whether it's time_varying (predicts supersession vs.
        contradiction on a subsequent proposal, SPEC §10.1)."""
        kb = _kb(tmp_path)
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    relations={
                        "employer": RelationDef(
                            name="employer",
                            target_concept="Organization",
                            cardinality="single",
                            required=True,
                            temporality="time_varying",
                            inverse="employees",
                        ),
                    },
                ),
                "Organization": ConceptDef(
                    name="Organization",
                    relations={
                        "employees": RelationDef(
                            name="employees",
                            target_concept="Person",
                            cardinality="many",
                        ),
                    },
                ),
            },
        )
        kb.backend.put_schema(schema)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.schema").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        concepts_by_name = {c["name"]: c for c in result["concepts"]}
        employer = concepts_by_name["Person"]["relations"][0]
        assert employer["name"] == "employer"
        assert employer["target_concept"] == "Organization"
        assert employer["cardinality"] == "single"
        assert employer["required"] is True
        assert employer["temporality"] == "time_varying"
        assert employer["inverse"] == "employees"

        employees = concepts_by_name["Organization"]["relations"][0]
        assert employees["cardinality"] == "many"
        assert employees["required"] is False
        assert employees["inverse"] is None

    def test_schema_respects_namespace_argument(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.schema").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0], namespace="other"
        )
        assert result == {"concepts": []}


# ---------------------------------------------------------------------------
# ontolith.get
# ---------------------------------------------------------------------------


class TestGetTool:
    def test_get_known_entity(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.get").fn(
            entity_id=entity.id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["entity"]["id"] == entity.id
        assert result["entity"]["concept"] == "Person"
        assert len(result["assertions"]) == 1
        assert result["assertions"][0]["value"] == "Ada"

    def test_get_unknown_entity_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.get").fn(
            entity_id="does-not-exist", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert "error" in result
        assert result["code"] == "NOT_FOUND"

    def test_get_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.get").fn(
            entity_id=entity.id, token="not-a-real-token"
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_get_excludes_non_active_assertions(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        kb.retract(active[0].id, HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.get").fn(
            entity_id=entity.id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
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

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["count"] == 2
        assert len(result["entities"]) == 2

    def test_query_with_filter(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        e1 = kb.create_entity("Person", author=HUMAN)
        e2 = kb.create_entity("Person", author=HUMAN)
        kb.propose(e1.id, "Person.name", "Ada", "Text", HUMAN)
        kb.propose(e2.id, "Person.name", "Grace", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            filters={"name": "Ada"},
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == e1.id

    def test_query_empty_concept_returns_empty(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Organization", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["count"] == 0
        assert result["entities"] == []

    def test_query_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person", token="not-a-real-token"
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_query_dunder_filter_key_returns_validation_error(self, tmp_path: Path) -> None:
        """A relation-traversal-shaped key fails loudly instead of returning an empty result (KI-030)."""
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            filters={"employer__name": "Acme Corp"},
        )
        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_query_semantic_restricts_to_indexed_entities(self, tmp_path: Path) -> None:
        """KI-058: `semantic` was previously unreachable from MCP at all.

        Pins that .semantic() is actually invoked, not ranking quality
        (that's QueryBuilder's own concern) - the second entity is created
        (and left unindexed) AFTER reindex(), so a real semantic search
        must exclude it (only the vector index's own entities are
        candidates); a no-op `semantic` that silently fell through to a
        plain, unranked entities() scan would include it (count=2), which
        is what makes this discriminating rather than vacuously passing on
        a single-entity KB."""
        kb = _kb(tmp_path)
        indexed = kb.create_entity("Person", author=HUMAN)
        kb.propose(indexed.id, "Person.name", "Ada Lovelace", "Text", HUMAN)
        kb.reindex()
        kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            semantic="Ada Lovelace",
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == indexed.id

    def test_query_unknown_namespace_argument_is_silently_dropped(self, tmp_path: Path) -> None:
        """KI-058 removed the tool's dead `namespace` parameter. Through the
        real schema-validated dispatch path (unlike `.fn()`, which every
        other test in this class uses and which bypasses the tool's JSON
        schema entirely), FastMCP drops arguments the schema doesn't
        declare rather than rejecting the call - so a caller still passing
        `namespace=` keeps succeeding, identically to before this KI, when
        it was already a silent no-op. Pins that this is genuinely
        non-breaking at the protocol level, not just at the Python level."""
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        async def _call() -> object:
            _, structured = await mcp.call_tool(
                "ontolith.query", {"concept": "Person", "token": token, "namespace": "other"}
            )
            return structured

        result = asyncio.run(_call())
        assert result == {
            "concept": "Person",
            "count": 1,
            "entities": [
                {
                    "id": "id-0",
                    "concept": "Person",
                    "natural_key": None,
                    "created_at": "2025-01-01T00:00:00+00:00",
                }
            ],
        }

    def test_query_as_of_excludes_entity_not_yet_existing_with_semantic(
        self, tmp_path: Path
    ) -> None:
        """Found while adding as_of/semantic coverage for KI-058: with no
        .where() filter, `_semantic_candidates()` previously ignored
        as_of_time entirely, so a semantic-only as_of query returned
        entities that didn't exist yet at that point in time. Fixed in the
        same PR (query/builder.py) since KI-058 is what first exposes this
        combination through any interface at all - REST/GraphQL don't have
        as_of wired in, so nothing could trigger it before now."""
        kb = _kb(tmp_path)
        existing = kb.create_entity("Person", author=HUMAN)
        kb.propose(existing.id, "Person.name", "Ada Lovelace", "Text", HUMAN)
        kb.reindex()

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)
        # Created AND reindexed only after T0 - a real as_of(T0) query must
        # exclude it even though it's in the vector index and matches
        # semantically.
        later = kb.create_entity("Person", author=HUMAN)
        kb.propose(later.id, "Person.name", "Ada Lovelace Jr", "Text", HUMAN)
        kb.reindex()

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            semantic="Ada Lovelace",
            as_of=T0.isoformat(),
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == existing.id

    def test_query_semantic_without_embedder_returns_validation_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.embedder = None
        kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            semantic="anything",
        )
        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_query_min_confidence_filters_entities(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        confident = kb.create_entity("Person", author=HUMAN)
        unsure = kb.create_entity("Person", author=HUMAN)
        kb.propose(confident.id, "Person.name", "Ada", "Text", HUMAN, confidence=0.9)
        kb.propose(unsure.id, "Person.name", "Bob", "Text", HUMAN, confidence=0.1)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            min_confidence=0.5,
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == confident.id

    def test_query_trust_at_least_filters_entities(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "trusted@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            trust_level=8,
        )
        trusted_entity = kb.create_entity("Person", author=HUMAN)
        untrusted_entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(trusted_entity.id, "Person.name", "Ada", "Text", "trusted@example.com")
        kb.propose(untrusted_entity.id, "Person.name", "Bob", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            trust_at_least=5,
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == trusted_entity.id

    def test_query_limit_caps_result_count(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)
        kb.create_entity("Person", author=HUMAN)
        kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            limit=2,
        )
        assert result["count"] == 2
        assert len(result["entities"]) == 2

    def test_query_as_of_reconstructs_past_state(self, tmp_path: Path) -> None:
        """KI-058: MCP is the first of the four shipped interfaces to expose
        bitemporal time-travel at all — REST/GraphQL/CLI still don't."""
        kb = _kb(tmp_path)
        kb.backend.put_schema(
            SchemaIR(
                namespace="default",
                version=1,
                concepts={
                    "Person": ConceptDef(
                        name="Person",
                        properties={
                            "title": PropertyDef(
                                name="title", value_type="Text", temporality="time_varying"
                            ),
                        },
                    ),
                },
            )
        )
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.title", "Engineer", "Text", HUMAN)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=180)
        kb.propose(entity.id, "Person.title", "Manager", "Text", HUMAN)

        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        past = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=token,
            filters={"title": "Engineer"},
            as_of=T0.isoformat(),
        )
        assert past["count"] == 1
        assert past["entities"][0]["id"] == entity.id

        now = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person", token=token, filters={"title": "Engineer"}
        )
        assert now["count"] == 0

    def test_query_malformed_as_of_returns_validation_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            as_of="not-a-timestamp",
        )
        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# ontolith.provenance
# ---------------------------------------------------------------------------


class TestProvenanceTool:
    def test_provenance_known_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN, confidence=0.95, source="wiki")
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id=assertions[0].id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["id"] == assertions[0].id
        assert result["author"] == HUMAN
        assert result["confidence"] == 0.95
        assert result["source"] == "wiki"
        assert result["subject"] == entity.id

    def test_provenance_surfaces_model_for_ai_authored_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "carol@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI, model="claude-sonnet-4"
        )
        kb.accept_proposal(proposal.id, "carol@example.com")
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id=assertions[0].id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["model"] == "claude-sonnet-4"

    def test_provenance_surfaces_review_events_after_accept(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "carol@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI, model="claude-sonnet-4"
        )
        kb.accept_proposal(proposal.id, "carol@example.com")
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id=assertions[0].id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert len(result["review_events"]) == 1
        assert result["review_events"][0]["type"] == "accept"
        assert result["review_events"][0]["actor"] == "carol@example.com"

    def test_provenance_review_events_empty_for_direct_write(self, tmp_path: Path) -> None:
        """A direct write (no proposal_id) has no review events - not an
        error, just an empty list."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id=assertion.id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["proposal_id"] is None
        assert result["review_events"] == []

    def test_provenance_unknown_assertion_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id="nonexistent", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert "error" in result
        assert result["code"] == "NOT_FOUND"

    def test_provenance_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id="nonexistent", token="not-a-real-token"
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_provenance_reachable_for_retracted_assertion(self, tmp_path: Path) -> None:
        """Provenance must be resolvable for non-active assertions too — that's
        the audit-trail use case (regression: backend.assertions() defaults to
        status='active' and previously hid retracted/flagged/superseded records).
        """
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        kb.retract(active[0].id, HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id=active[0].id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["id"] == active[0].id
        assert result["status"] == "retracted"

    def test_provenance_superseded_ids_include_all_concurrent_predecessors(
        self, tmp_path: Path
    ) -> None:
        """KI-008: supersedes alone only records the first predecessor when one
        incoming assertion supersedes several concurrently-overlapping ones."""
        kb = _kb(tmp_path)
        kb.backend.put_schema(
            SchemaIR(
                namespace="default",
                version=1,
                concepts={
                    "Person": ConceptDef(
                        name="Person",
                        properties={
                            "employer": PropertyDef(
                                name="employer", value_type="Text", temporality="time_varying"
                            ),
                        },
                    ),
                },
            )
        )
        entity = kb.create_entity("Person", author=HUMAN)

        a1 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.employer",
            value_kind="literal",
            value_type="Text",
            value="Acme Corp",
            author=HUMAN,
            asserted_at=T0,
            valid_from=T0,
        )
        kb.backend.put_assertion(a1)
        a2 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.employer",
            value_kind="literal",
            value_type="Text",
            value="Beta Inc",
            author=HUMAN,
            asserted_at=T0,
            valid_from=T0,
        )
        kb.backend.put_assertion(a2)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=180)
        kb.propose(entity.id, "Person.employer", "Gamma Ltd", "Text", HUMAN)
        active = kb.assertions(subject=entity.id, predicate="Person.employer", status="active")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id=active[0].id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["supersedes"] in {a1.id, a2.id}
        assert set(result["superseded_ids"]) == {a1.id, a2.id}


# ---------------------------------------------------------------------------
# ontolith.propose
# ---------------------------------------------------------------------------


class TestProposeTool:
    def test_propose_human_auto_accepted(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert result["proposal"]["state"] == "auto_accepted"
        assert result["decision"] == "AutoAccept"

    def test_propose_ai_without_model_returns_validation_error(self, tmp_path: Path) -> None:
        """model is required for ai-kind authors (SPEC §7.4) — enforced even
        via the MCP tool wrapper, not just the SDK method directly."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token=kb.issue_token(AI, author=ADMIN)[0],
        )
        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_propose_ai_requires_review(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token=kb.issue_token(AI, author=ADMIN)[0],
            model="test-model-v1",
        )

        assert result["proposal"]["state"] == "require_review"
        assert result["decision"] == "RequireReview"

    def test_propose_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        """A garbage/unknown token is rejected — this is the core fix: the
        acting principal can no longer be spoofed by naming any principal ID."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token="not-a-real-token",
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_propose_revoked_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        token, credential_id = kb.issue_token(HUMAN, author=ADMIN)
        kb.revoke_token(credential_id, author=ADMIN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token=token,
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_propose_unauthorized_delegation_returns_capability_error(self, tmp_path: Path) -> None:
        """acting_as a principal that isn't the author's owner is rejected (ADR-0003)."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.create_principal(
            "carol@example.com", kind="human", auth_method="oidc", default_capability="write"
        )

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token=kb.issue_token(AI, author=ADMIN)[0],  # owner=alice
            acting_as="carol@example.com",  # not AI's owner
            model="test-model-v1",
        )
        assert "error" in result
        assert result["code"] == "CAPABILITY_ERROR"

    def test_propose_does_not_expose_direct_write(self, tmp_path: Path) -> None:
        """MCP propose tool must not bypass policy — AI assertions need review."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token=kb.issue_token(AI, author=ADMIN)[0],
            model="test-model-v1",
        )

        # No assertion written yet — still pending review
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active == []

    def test_propose_with_target_creates_relation(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        person = kb.create_entity("Person", author=HUMAN)
        org = kb.create_entity("Organization", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=person.id,
            predicate="Person.employer",
            target=org.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert result["proposal"]["state"] == "auto_accepted"
        active = kb.assertions(subject=person.id, predicate="Person.employer", status="active")
        assert len(active) == 1
        assert active[0].value_kind == "ref"
        assert active[0].value == org.id

    def test_propose_neither_value_nor_target_returns_validation_error(
        self, tmp_path: Path
    ) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_propose_both_value_and_target_returns_validation_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        person = kb.create_entity("Person", author=HUMAN)
        org = kb.create_entity("Organization", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=person.id,
            predicate="Person.employer",
            value="Acme",
            value_type="Text",
            target=org.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_no_write_tool_registered(self, tmp_path: Path) -> None:
        """ADR-0008: no direct write, update, or delete tool must be registered."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        forbidden = {"ontolith.write", "ontolith.update", "ontolith.delete", "ontolith.assert"}
        assert tool_names.isdisjoint(forbidden)

    def test_all_required_tools_registered(self, tmp_path: Path) -> None:
        """ADR-0008 plus resubmit (KI-027) and retract (KI-057, ADR-0039):
        all 8 required tools must be present."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        required = {
            "ontolith.schema",
            "ontolith.get",
            "ontolith.query",
            "ontolith.provenance",
            "ontolith.propose",
            "ontolith.flag_contradiction",
            "ontolith.resubmit",
            "ontolith.retract",
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

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=assertions[0].id,
            assertion_id_b=a2.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert "contradiction_id" in result
        assert result["action"] == "created"
        assert set(result["member_ids"]) == {assertions[0].id, a2.id}
        assert result["raised_by"] == HUMAN

    def test_rationale_is_readable_back_in_rationale_history(self, tmp_path: Path) -> None:
        """KI-075: `rationale` (KI-071) must round-trip through this tool's
        own response, not just be recorded server-side and unreachable."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

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

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=assertions[0].id,
            assertion_id_b=a2.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            rationale="Sources disagree",
        )

        assert result["rationale_history"] == [
            {"rationale": "Sources disagree", "actor": HUMAN, "at": T0.isoformat()}
        ]

    def test_flag_different_predicates_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        kb.propose(entity.id, "Person.born", "1815-12-10", "Date", HUMAN)

        all_a = kb.assertions(subject=entity.id, status="active")
        name_a = next(a for a in all_a if a.predicate == "Person.name")
        born_a = next(a for a in all_a if a.predicate == "Person.born")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=name_a.id,
            assertion_id_b=born_a.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )
        assert "error" in result

    def test_flag_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a="a1",
            assertion_id_b="a2",
            token="not-a-real-token",
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_flag_read_only_principal_returns_capability_error(self, tmp_path: Path) -> None:
        """Previously flag_contradiction had NO capability check at all — any
        principal, even read-only, could flag assertions out of default query
        results. This is the regression test for that HIGH finding."""
        kb = _kb(tmp_path)
        kb.create_principal(
            "readonly@example.com", kind="human", auth_method="oidc", default_capability="read"
        )
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

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

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=assertions[0].id,
            assertion_id_b=a2.id,
            token=kb.issue_token("readonly@example.com", author=ADMIN)[0],
        )
        assert "error" in result
        assert result["code"] == "CAPABILITY_ERROR"

    def test_flag_assertion_a_not_found_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a="nonexistent",
            assertion_id_b=assertions[0].id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )
        assert "error" in result
        assert result["code"] == "NOT_FOUND"

    def test_flag_assertion_b_not_found_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=assertions[0].id,
            assertion_id_b="nonexistent",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )
        assert "error" in result
        assert result["code"] == "NOT_FOUND"

    def test_flag_extends_existing_contradiction(self, tmp_path: Path) -> None:
        """A third conflicting assertion extends the open contradiction rather than
        creating a new one, and already-flagged members are not re-flagged.
        """
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        # First two proposals conflict via normal conflict routing -> open contradiction
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        kb.propose(entity.id, "Person.name", "Ava", "Text", HUMAN)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert len(flagged) == 2

        from ontolith.core import Assertion

        a3 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Eve",
            author=HUMAN,
            asserted_at=T0,
            status="active",
        )
        kb.backend.put_assertion(a3)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=flagged[0].id,
            assertion_id_b=a3.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert result["action"] == "extended"
        still_flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert {a.id for a in still_flagged} == {flagged[0].id, flagged[1].id, a3.id}


# ---------------------------------------------------------------------------
# ontolith.resubmit
# ---------------------------------------------------------------------------


class TestResubmitTool:
    def test_resubmit_moves_through_policy_again(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="test-model-v1")
        kb.request_changes(proposal.id, REVIEWER)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.resubmit").fn(
            proposal_id=proposal.id,
            token=kb.issue_token(AI, author=ADMIN)[0],
        )

        # AI proposals always route to require_review (ADR-0003).
        assert result["proposal"]["state"] == "require_review"
        assert result["decision"] == "RequireReview"

    def test_resubmit_unrelated_principal_returns_capability_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="test-model-v1")
        kb.request_changes(proposal.id, REVIEWER)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.resubmit").fn(
            proposal_id=proposal.id,
            token=kb.issue_token(REVIEWER, author=ADMIN)[0],
        )

        assert "error" in result
        assert result["code"] == "CAPABILITY_ERROR"

    def test_resubmit_unknown_proposal_returns_not_found(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.resubmit").fn(
            proposal_id="nonexistent-id",
            token=kb.issue_token(AI, author=ADMIN)[0],
        )

        assert "error" in result
        assert result["code"] == "NOT_FOUND"

    def test_resubmit_wrong_state_returns_validation_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="test-model-v1")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.resubmit").fn(
            proposal_id=proposal.id,
            token=kb.issue_token(AI, author=ADMIN)[0],
        )

        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_resubmit_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="test-model-v1")
        kb.request_changes(proposal.id, REVIEWER)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.resubmit").fn(
            proposal_id=proposal.id,
            token="not-a-real-token",
        )

        assert "error" in result
        assert result["code"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# ontolith.retract (KI-057, ADR-0039)
# ---------------------------------------------------------------------------


class TestRetractTool:
    def test_write_capability_auto_accepts(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id=assertion.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert result["proposal"]["state"] == "auto_accepted"
        assert result["decision"] == "AutoAccept"
        retracted = kb.assertions(subject=entity.id, status=None)[0]
        assert retracted.status == "retracted"

    def test_ai_requires_review(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id=assertion.id,
            token=kb.issue_token(AI, author=ADMIN)[0],
        )

        assert result["proposal"]["state"] == "require_review"
        assert result["decision"] == "RequireReview"

    def test_acting_as_delegation(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "delegate@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            owner=HUMAN,
        )
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id=assertion.id,
            token=kb.issue_token("delegate@example.com", author=ADMIN)[0],
            acting_as=HUMAN,
        )

        assert result["proposal"]["acting_as"] == HUMAN

    def test_unknown_acting_as_returns_auth_error(self, tmp_path: Path) -> None:
        """Distinct from an invalid token: the token resolves fine, but the
        requested acting_as principal doesn't exist - _resolve_delegation
        raises AuthError for this, not CapabilityError."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id=assertion.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            acting_as="nonexistent-principal",
        )

        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_retract_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id=assertion.id,
            token="not-a-real-token",
        )

        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_party_to_contradiction_returns_capability_error(self, tmp_path: Path) -> None:
        """KI-033: a party to an open contradiction can't retract the
        opposing member, even at write capability (auto-accept would
        otherwise let them unilaterally settle their own dispute)."""
        kb = _kb(tmp_path)
        kb.create_principal("wendy@example.com", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author=HUMAN)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", "wendy@example.com")
        opposing = next(
            a
            for a in kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
            if a.author == "wendy@example.com"
        )

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id=opposing.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert "error" in result
        assert result["code"] == "CAPABILITY_ERROR"


# ---------------------------------------------------------------------------
# Bearer-token transport (KI-067): Authorization header vs. `token` argument
# ---------------------------------------------------------------------------


class TestBearerTokenTransport:
    """The `token` argument still works everywhere (every test above uses
    it, unchanged) — these pin the new SSE/streamable-HTTP behavior: an
    `Authorization` header, when the transport supplies one, is preferred
    over the argument, keeping a live credential out of the calling model's
    own tool-call arguments."""

    def test_header_only_authenticates_without_token_argument(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(f"Bearer {token}"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn()

        assert result == {"concepts": []}

    def test_header_takes_priority_over_token_argument(self, tmp_path: Path) -> None:
        """A valid header authenticates even when the `token` argument is
        garbage — the header wins, not the argument."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(f"Bearer {token}"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token="not-a-real-token")

        assert result == {"concepts": []}

    def test_unresolvable_header_token_does_not_fall_back_to_valid_argument(
        self, tmp_path: Path
    ) -> None:
        """The strongest fail-closed case: a well-formed header carrying a
        token that doesn't resolve to any principal must fail — even
        alongside a `token` argument that IS valid. The header winning only
        when it happens to work would defeat the point (a caller could
        smuggle a bad header past authentication by also supplying a good
        argument); the header must win, full stop, once it's well-formed."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        valid_token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request("Bearer not-a-real-token"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token=valid_token)

        assert result == {"error": "Invalid or revoked token", "code": "AUTH_ERROR", "detail": {}}

    def test_no_header_falls_back_to_token_argument(self, tmp_path: Path) -> None:
        """A live HTTP request with no Authorization header at all (as
        opposed to no HTTP request/context existing) still falls back to
        the argument, same as stdio."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(None):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token=token)

        assert result == {"concepts": []}

    def test_malformed_header_fails_closed_ignoring_token_argument(self, tmp_path: Path) -> None:
        """A header present but not a well-formed `Bearer <token>` value
        (wrong scheme here) does NOT fall back to the argument, even though
        the argument is valid — silently falling back on a malformed header
        would reopen the exposure KI-067 removes. Fails closed instead."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request("Basic dXNlcjpwYXNz"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token=token)

        assert result == {
            "error": "Malformed Authorization header",
            "code": "AUTH_ERROR",
            "detail": {},
        }

    def test_empty_bearer_value_fails_closed(self, tmp_path: Path) -> None:
        """`Authorization: Bearer` with no value at all (not just a wrong
        scheme) also fails closed rather than falling back."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request("Bearer"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token=token)

        assert result == {
            "error": "Malformed Authorization header",
            "code": "AUTH_ERROR",
            "detail": {},
        }

    def test_uppercase_bearer_scheme_authenticates(self, tmp_path: Path) -> None:
        """The scheme match is case-insensitive (RFC 7235) — pins the
        `.lower()` explicitly, not just implicitly via the lowercase-scheme
        tests above."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(f"BEARER {token}"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn()

        assert result == {"concepts": []}

    def test_extra_whitespace_before_token_is_stripped(self, tmp_path: Path) -> None:
        """`Bearer  <token>` (double space, RFC 7235 permits BWS) still
        authenticates instead of silently failing closed on a value with
        leading whitespace."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(f"Bearer  {token}"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn()

        assert result == {"concepts": []}

    def test_no_header_and_no_token_argument_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)

        with _http_request(None):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn()

        assert result == {"error": "No bearer token provided", "code": "AUTH_ERROR", "detail": {}}

    def test_no_context_and_no_token_argument_returns_auth_error(self, tmp_path: Path) -> None:
        """Outside any request context at all (e.g. stdio, or a direct
        `.fn()` call as every other test in this file makes) — distinct
        from test_no_header_and_no_token_argument_returns_auth_error's live
        HTTP request with an absent header."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)

        result = mcp._tool_manager.get_tool("ontolith.schema").fn()

        assert result == {"error": "No bearer token provided", "code": "AUTH_ERROR", "detail": {}}

    def test_header_authenticates_a_write_tool(self, tmp_path: Path) -> None:
        """Not just the read tools — the `author = auth_provider.resolve
        (token).id` shape (propose/retract/flag_contradiction/resubmit) is
        wired the same way."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(f"Bearer {token}"):
            result = mcp._tool_manager.get_tool("ontolith.propose").fn(
                subject=entity.id, predicate="Person.name", value="Ada", value_type="Text"
            )

        assert result["proposal"]["state"] == "auto_accepted"

    def test_no_token_returns_auth_error_for_every_tool(self, tmp_path: Path) -> None:
        """Each of the 8 tools gained its own `if token is None` branch
        (KI-067) — not just ontolith.schema's, exercised above."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        proposal, _ = kb.propose(
            entity.id, "Person.born", "1815-12-10", "Date", AI, model="test-model-v1"
        )
        kb.request_changes(proposal.id, REVIEWER)

        mcp, _ = _server(kb)
        calls = {
            "ontolith.get": {"entity_id": entity.id},
            "ontolith.query": {"concept": "Person"},
            "ontolith.provenance": {"assertion_id": assertion.id},
            "ontolith.propose": {
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ada",
                "value_type": "Text",
            },
            "ontolith.retract": {"assertion_id": assertion.id},
            "ontolith.flag_contradiction": {
                "assertion_id_a": assertion.id,
                "assertion_id_b": assertion.id,
            },
            "ontolith.resubmit": {"proposal_id": proposal.id},
        }
        for tool_name, kwargs in calls.items():
            result = mcp._tool_manager.get_tool(tool_name).fn(**kwargs)
            assert result == {
                "error": "No bearer token provided",
                "code": "AUTH_ERROR",
                "detail": {},
            }, tool_name

    def test_header_authenticates_over_real_streamable_http_transport(self, tmp_path: Path) -> None:
        """Every test above drives `_bearer_token` through `_http_request`'s
        faked `RequestContext` — fast and deterministic, but only as good as
        the fake staying an accurate stand-in for the SDK's real wiring. This
        one instead runs the real streamable-HTTP ASGI app end to end."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        result = asyncio.run(
            _call_tool_over_real_transport(mcp, "ontolith.schema", {}, f"Bearer {token}")
        )

        assert result == {"concepts": []}

    def test_no_header_falls_back_to_argument_over_real_transport(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        result = asyncio.run(
            _call_tool_over_real_transport(mcp, "ontolith.schema", {"token": token}, None)
        )

        assert result == {"concepts": []}

    def test_malformed_header_fails_closed_over_real_transport(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        result = asyncio.run(
            _call_tool_over_real_transport(
                mcp, "ontolith.schema", {"token": token}, "Basic dXNlcjpwYXNz"
            )
        )

        assert result == {
            "error": "Malformed Authorization header",
            "code": "AUTH_ERROR",
            "detail": {},
        }


# ---------------------------------------------------------------------------
# Blanket OntolithError handling (KI-074)
# ---------------------------------------------------------------------------


class _FailingSchemaBackend:
    """Wraps a real StorageBackend, making get_schema raise a StorageError
    whose message interpolates fake internal detail - injects a failure
    from a taxonomy member no tool previously caught by name (StorageError
    was never in any per-tool except clause pre-KI-074), to prove the new
    blanket handler actually reaches it. Forwards every other call to the
    wrapped backend via __getattr__."""

    def __init__(self, real: object) -> None:
        self._real = real

    def get_schema(self, namespace: str) -> object:
        from ontolith.core.errors import StorageError

        raise StorageError("sqlite3.OperationalError: database is locked (fd=7, pid=12345)")

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class TestBlanketErrorHandling:
    """KI-074: every tool now catches the full OntolithError taxonomy via
    one shared _error_response() helper, not just the 4 types each tool
    used to hand-catch — and StorageError/PluginError messages are
    redacted the same way REST/GraphQL's own blanket handlers already do."""

    def test_storage_error_reaches_the_blanket_handler_redacted(self, tmp_path: Path) -> None:
        from ontolith.store.sqlite import SQLiteBackend

        real = SQLiteBackend(tmp_path / "test.db")
        try:
            kb = Ontology(
                _FailingSchemaBackend(real),  # type: ignore[arg-type]
                clock=FixedClock(T0),
                id_provider=FixedIdProvider([f"id-{i}" for i in range(10)]),
            )
            kb.create_principal(HUMAN, kind="human", default_capability="admin")
            mcp, _ = _server(kb)

            result = mcp._tool_manager.get_tool("ontolith.schema").fn(
                token=kb.issue_token(HUMAN, author=HUMAN)[0]
            )

            assert result["code"] == "STORAGE_ERROR"
            assert result["error"] == "An internal error occurred"
            assert "database is locked" not in result["error"]
            assert result["detail"] == {}
        finally:
            real.close()

    def test_capability_error_still_reaches_response_via_blanket_handler(
        self, tmp_path: Path
    ) -> None:
        """CapabilityError was one of the 4 types each tool already caught
        by name pre-KI-074 - pins that consolidating every except clause
        into one `except OntolithError` didn't silently drop coverage for
        the types that already worked."""
        kb = _kb(tmp_path)
        kb.create_principal("readonly@example.com", kind="human", default_capability="read")
        entity = kb.create_entity("Person", author=HUMAN)
        assertion_a = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        assertion_b = kb.assert_literal(entity.id, "Person.name", "Ava", "Text", HUMAN)
        mcp, _ = _server(kb)

        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=assertion_a.id,
            assertion_id_b=assertion_b.id,
            token=kb.issue_token("readonly@example.com", author=ADMIN)[0],
        )

        assert result["code"] == "CAPABILITY_ERROR"
        assert "detail" in result

    def test_non_redacted_error_message_passes_through_unchanged(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)

        result = mcp._tool_manager.get_tool("ontolith.get").fn(
            entity_id="does-not-exist", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["code"] == "NOT_FOUND"
        assert result["error"] == "Entity 'does-not-exist' not found"
        assert result["detail"] == {}

    def test_retract_unknown_assertion_is_not_found_not_redacted(self, tmp_path: Path) -> None:
        """Regression (KI-074 review): before the Ontology.retract() fix,
        an unknown assertion_id surfaced as a StorageError, which
        _REDACT_MESSAGE_FOR then hid behind "An internal error occurred"
        — a client-supplied bad ID must stay actionable, not become
        indistinguishable from a genuine storage fault."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)

        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id="does-not-exist", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["code"] == "NOT_FOUND"
        assert result["error"] == "Assertion not found: does-not-exist"
        assert result["detail"] == {}
