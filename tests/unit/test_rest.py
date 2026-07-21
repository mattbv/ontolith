"""Unit tests for the REST interface (SPEC §14.3, ADR-0021).

Mirrors test_mcp_server.py's fixtures and structure: verifies each route's
response shape and that every route — reads included — requires a bearer
token resolved via AuthProvider (never a caller-asserted principal ID).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.identity.token_auth import TokenAuthProvider
from ontolith.interfaces.rest import create_rest_app
from ontolith.schema.ir import ConceptDef, PropertyDef, SchemaIR

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


def _client(kb: Ontology) -> tuple[TestClient, TokenAuthProvider]:
    auth_provider = TokenAuthProvider(kb.backend)
    return TestClient(create_rest_app(kb, auth_provider)), auth_provider


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# GET /schema
# ---------------------------------------------------------------------------


class TestSchemaRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.get("/schema")
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_ERROR"

    def test_rejects_invalid_token(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.get("/schema", headers=_auth("not-a-real-token"))
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_ERROR"

    def test_returns_empty_when_no_schema_stored(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/schema", headers=_auth(token))
        assert response.status_code == 200
        assert response.json() == {"namespace": None, "version": None, "concepts": []}

    def test_returns_concepts_and_properties(self, tmp_path: Path) -> None:
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
                    },
                ),
            },
        )
        kb.backend.put_schema(schema)

        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/schema", headers=_auth(token))

        body = response.json()
        assert body["namespace"] == "default"
        assert body["version"] == 1
        assert len(body["concepts"]) == 1
        person = body["concepts"][0]
        assert person["name"] == "Person"
        props_by_name = {p["name"]: p for p in person["properties"]}
        assert props_by_name["name"]["type"] == "Text"
        assert props_by_name["name"]["required"] is True
        assert props_by_name["employer"]["temporality"] == "time_varying"

    def test_respects_namespace_argument(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/schema", params={"namespace": "other"}, headers=_auth(token))
        assert response.status_code == 200
        assert response.json() == {"namespace": None, "version": None, "concepts": []}


# ---------------------------------------------------------------------------
# GET /entities/{entity_id}
# ---------------------------------------------------------------------------


class TestEntityRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        response = client.get(f"/entities/{entity.id}")
        assert response.status_code == 401

    def test_known_entity(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)

        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get(f"/entities/{entity.id}", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["entity"]["id"] == entity.id
        assert body["entity"]["concept"] == "Person"
        assert len(body["assertions"]) == 1
        assert body["assertions"][0]["value"] == "Ada"

    def test_unknown_entity_returns_404(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/entities/does-not-exist", headers=_auth(token))
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    def test_excludes_non_active_assertions(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        kb.retract(active[0].id, HUMAN)

        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get(f"/entities/{entity.id}", headers=_auth(token))
        assert response.json()["assertions"] == []


# ---------------------------------------------------------------------------
# POST /query
# ---------------------------------------------------------------------------


class TestQueryRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.post("/query", json={"concept": "Person"})
        assert response.status_code == 401

    def test_all_of_concept(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)
        kb.create_entity("Person", author=HUMAN)
        kb.create_entity("Organization", author=HUMAN)

        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post("/query", json={"concept": "Person"}, headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 2
        assert len(body["entities"]) == 2

    def test_with_filter(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        e1 = kb.create_entity("Person", author=HUMAN)
        e2 = kb.create_entity("Person", author=HUMAN)
        kb.propose(e1.id, "Person.name", "Ada", "Text", HUMAN)
        kb.propose(e2.id, "Person.name", "Grace", "Text", HUMAN)

        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/query",
            json={"concept": "Person", "filters": {"name": "Ada"}},
            headers=_auth(token),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["entities"][0]["id"] == e1.id

    def test_limit_is_applied(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        for _ in range(3):
            kb.create_entity("Person", author=HUMAN)

        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/query", json={"concept": "Person", "limit": 2}, headers=_auth(token)
        )

        assert response.status_code == 200
        assert response.json()["count"] == 2

    def test_empty_concept_returns_empty(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post("/query", json={"concept": "Organization"}, headers=_auth(token))
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 0
        assert body["entities"] == []


# ---------------------------------------------------------------------------
# GET /provenance/{assertion_id}
# ---------------------------------------------------------------------------


class TestProvenanceRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        client, _ = _client(kb)
        response = client.get(f"/provenance/{assertion.id}")
        assert response.status_code == 401

    def test_known_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN, confidence=0.95, source="wiki")
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get(f"/provenance/{assertions[0].id}", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == assertions[0].id
        assert body["author"] == HUMAN
        assert body["confidence"] == 0.95
        assert body["source"] == "wiki"
        assert body["subject"] == entity.id

    def test_surfaces_review_events_after_accept(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI, model="claude-sonnet-4"
        )
        kb.accept_proposal(proposal.id, REVIEWER)
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get(f"/provenance/{assertions[0].id}", headers=_auth(token))

        body = response.json()
        assert len(body["review_events"]) == 1
        assert body["review_events"][0]["type"] == "accept"
        assert body["review_events"][0]["actor"] == REVIEWER

    def test_review_events_empty_for_direct_write(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get(f"/provenance/{assertion.id}", headers=_auth(token))

        body = response.json()
        assert body["proposal_id"] is None
        assert body["review_events"] == []

    def test_unknown_assertion_returns_404(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/provenance/nonexistent", headers=_auth(token))
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    def test_reachable_for_retracted_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        kb.retract(active[0].id, HUMAN)

        client, _ = _client(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get(f"/provenance/{active[0].id}", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == active[0].id
        assert body["status"] == "retracted"
