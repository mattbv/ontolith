"""Unit tests for the GraphQL interface (SPEC §14.3, ADR-0037).

Mirrors test_rest.py's fixtures and structure, adapted for GraphQL's single
POST /graphql endpoint and errors[] response shape instead of REST's HTTP
status codes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.core.errors import StorageError
from ontolith.identity.token_auth import TokenAuthProvider
from ontolith.interfaces.graphql import create_graphql_app
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


def _client(kb: Ontology) -> tuple[TestClient, TokenAuthProvider]:
    auth_provider = TokenAuthProvider(kb.backend)
    return TestClient(create_graphql_app(kb, auth_provider)), auth_provider


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _gql(
    client: TestClient,
    query: str,
    *,
    variables: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/graphql", json={"query": query, "variables": variables or {}}, headers=headers
    )
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def _error_codes(body: dict[str, Any]) -> list[str]:
    return [e["extensions"]["code"] for e in body.get("errors", [])]


# ---------------------------------------------------------------------------
# create_graphql_app(graphql_ide=...)
# ---------------------------------------------------------------------------


class TestGraphqlIde:
    def test_ide_enabled_by_default(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.get("/graphql", headers={"accept": "text/html"})
        assert response.status_code == 200

    def test_ide_can_be_disabled(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        auth_provider = TokenAuthProvider(kb.backend)
        app = create_graphql_app(kb, auth_provider, graphql_ide=None)
        client = TestClient(app)
        response = client.get("/graphql", headers={"accept": "text/html"})
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuth:
    def test_introspection_works_unauthenticated(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        body = _gql(client, "{ __schema { queryType { name } } }")
        assert "errors" not in body
        assert body["data"]["__schema"]["queryType"]["name"] == "Query"

    def test_real_field_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        body = _gql(client, "{ schema { version } }")
        assert _error_codes(body) == ["AUTH_ERROR"]
        assert body["data"] is None

    def test_rejects_invalid_token(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        body = _gql(client, "{ schema { version } }", headers=_auth("not-a-real-token"))
        assert _error_codes(body) == ["AUTH_ERROR"]

    def test_storage_error_redacts_internal_detail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)

        def _raise_storage_error(*args: object, **kwargs: object) -> None:
            raise StorageError("sqlite3.OperationalError: table assertion has no column baz")

        monkeypatch.setattr(kb.backend, "get_schema", _raise_storage_error)

        body = _gql(client, "{ schema { version } }", headers=_auth(token))
        assert _error_codes(body) == ["STORAGE_ERROR"]
        message = body["errors"][0]["message"]
        assert message == "An internal error occurred"
        assert "sqlite3" not in message
        assert "baz" not in str(body)

    def test_non_ontolith_exception_falls_through_to_default_handling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolver raising a plain (non-OntolithError) exception - e.g. a
        real bug - must not be mistaken for a domain error: no `code`
        extension is fabricated for it, and the default
        strawberry.Schema.process_errors handling (logging) still runs."""
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)

        def _raise_runtime_error(*args: object, **kwargs: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(kb.backend, "get_schema", _raise_runtime_error)

        body = _gql(client, "{ schema { version } }", headers=_auth(token))
        [error] = body["errors"]
        assert error.get("extensions", {}).get("code") is None
        assert "boom" in error["message"]


# ---------------------------------------------------------------------------
# Query.schema
# ---------------------------------------------------------------------------


class TestSchemaQuery:
    def test_returns_empty_when_no_schema_stored(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(
            client, "{ schema { namespace version concepts { name } } }", headers=_auth(token)
        )
        assert body["data"]["schema"] == {"namespace": None, "version": None, "concepts": []}

    def test_returns_concepts_properties_and_relations(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={"name": PropertyDef(name="name", value_type="Text", required=True)},
                    relations={
                        "employer": RelationDef(
                            name="employer",
                            target_concept="Organization",
                            temporality="time_varying",
                            inverse="employees",
                        )
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )
        kb.backend.put_schema(schema)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        query = """
        {
          schema {
            namespace
            version
            concepts {
              name
              properties { name type required cardinality }
              relations { name targetConcept temporality inverse }
            }
          }
        }
        """
        body = _gql(client, query, headers=_auth(token))
        concept = body["data"]["schema"]["concepts"][0]
        assert concept["name"] == "Person"
        assert concept["properties"][0] == {
            "name": "name",
            "type": "Text",
            "required": True,
            "cardinality": "single",
        }
        assert concept["relations"][0]["targetConcept"] == "Organization"
        assert concept["relations"][0]["temporality"] == "time_varying"
        assert concept["relations"][0]["inverse"] == "employees"

    def test_respects_namespace_argument(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(
            client,
            '{ schema(namespace: "other") { namespace version } }',
            headers=_auth(token),
        )
        assert body["data"]["schema"] == {"namespace": None, "version": None}


# ---------------------------------------------------------------------------
# Query.entity
# ---------------------------------------------------------------------------


class TestEntityQuery:
    def test_unknown_entity_returns_not_found(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(client, '{ entity(id: "nope") { id } }', headers=_auth(token))
        assert _error_codes(body) == ["NOT_FOUND"]

    def test_known_entity_with_nested_assertions(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        query = f'{{ entity(id: "{entity.id}") {{ id concept assertions {{ predicate value }} }} }}'
        body = _gql(client, query, headers=_auth(token))
        result = body["data"]["entity"]
        assert result["id"] == entity.id
        assert result["concept"] == "Person"
        assert result["assertions"] == [{"predicate": "Person.name", "value": "Ada"}]

    def test_assertions_not_fetched_when_not_requested(self, tmp_path: Path) -> None:
        """The nested `assertions` field is resolved lazily - a query that
        only asks for entity fields must not touch backend.assertions()."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        called = False
        original = kb.backend.assertions

        def _tracking(*args: object, **kwargs: object) -> object:
            nonlocal called
            called = True
            return original(*args, **kwargs)  # type: ignore[arg-type]

        kb.backend.assertions = _tracking  # type: ignore[method-assign]
        body = _gql(client, f'{{ entity(id: "{entity.id}") {{ id }} }}', headers=_auth(token))
        assert body["data"]["entity"]["id"] == entity.id
        assert called is False


# ---------------------------------------------------------------------------
# Query.query
# ---------------------------------------------------------------------------


class TestQueryField:
    def test_filters_and_limit(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        e1 = kb.create_entity("Person", author=HUMAN)
        kb.propose(e1.id, "Person.name", "Ada", "Text", HUMAN)
        e2 = kb.create_entity("Person", author=HUMAN)
        kb.propose(e2.id, "Person.name", "Bob", "Text", HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        query = """
        query($filters: [FilterInput!]) {
          query(concept: "Person", filters: $filters) {
            concept
            count
            entities { id naturalKey }
          }
        }
        """
        body = _gql(
            client,
            query,
            variables={"filters": [{"key": "name", "value": "Ada"}]},
            headers=_auth(token),
        )
        result = body["data"]["query"]
        assert result["concept"] == "Person"
        assert result["count"] == 1
        assert result["entities"][0]["id"] == e1.id

    def test_unfiltered_limit(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)
        kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(
            client,
            '{ query(concept: "Person", limit: 1) { count entities { id } } }',
            headers=_auth(token),
        )
        assert body["data"]["query"]["count"] == 1

    def test_semantic_ranks_by_similarity(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada Lovelace", "Text", HUMAN)
        kb.reindex()

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(
            client,
            '{ query(concept: "Person", semantic: "Ada Lovelace") { count entities { id } } }',
            headers=_auth(token),
        )
        result = body["data"]["query"]
        assert result["count"] == 1
        assert result["entities"][0]["id"] == entity.id

    def test_min_confidence_filters_entities(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        confident = kb.create_entity("Person", author=HUMAN)
        unsure = kb.create_entity("Person", author=HUMAN)
        kb.propose(confident.id, "Person.name", "Ada", "Text", HUMAN, confidence=0.9)
        kb.propose(unsure.id, "Person.name", "Bob", "Text", HUMAN, confidence=0.1)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(
            client,
            '{ query(concept: "Person", minConfidence: 0.5) { count entities { id } } }',
            headers=_auth(token),
        )
        result = body["data"]["query"]
        assert result["count"] == 1
        assert result["entities"][0]["id"] == confident.id

    def test_trust_at_least_filters_entities(self, tmp_path: Path) -> None:
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

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(
            client,
            '{ query(concept: "Person", trustAtLeast: 5) { count entities { id } } }',
            headers=_auth(token),
        )
        result = body["data"]["query"]
        assert result["count"] == 1
        assert result["entities"][0]["id"] == trusted_entity.id


# ---------------------------------------------------------------------------
# Query.provenance
# ---------------------------------------------------------------------------


class TestProvenanceQuery:
    def test_unknown_assertion_returns_not_found(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(client, '{ provenance(assertionId: "nope") { id } }', headers=_auth(token))
        assert _error_codes(body) == ["NOT_FOUND"]

    def test_known_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _decision = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        [assertion] = kb.backend.assertions(subject=entity.id, status="active")

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        query = (
            f'{{ provenance(assertionId: "{assertion.id}") '
            "{ id subject predicate value proposalId } }"
        )
        body = _gql(client, query, headers=_auth(token))
        result = body["data"]["provenance"]
        assert result["id"] == assertion.id
        assert result["subject"] == entity.id
        assert result["value"] == "Ada"
        assert result["proposalId"] == proposal.id


# ---------------------------------------------------------------------------
# Query.proposals / Query.contradictions / Query.principals
# ---------------------------------------------------------------------------


class TestProposalsQuery:
    def test_defaults_to_require_review(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="gpt-test")

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(client, "{ proposals { id state } }", headers=_auth(token))
        assert len(body["data"]["proposals"]) == 1
        assert body["data"]["proposals"][0]["state"] == "require_review"

    def test_all_sentinel_returns_every_state(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)  # auto-accepted

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(client, '{ proposals(state: "all") { id state } }', headers=_auth(token))
        assert len(body["data"]["proposals"]) == 1
        assert body["data"]["proposals"][0]["state"] == "auto_accepted"


class TestContradictionsQuery:
    def test_defaults_to_open(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        e = kb.create_entity("Person", author=HUMAN)
        kb.assert_literal(e.id, "Person.name", "Ada", "Text", author=HUMAN)
        kb.assert_literal(e.id, "Person.name", "Ida", "Text", author=ADMIN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(client, "{ contradictions { id state } }", headers=_auth(token))
        assert len(body["data"]["contradictions"]) == 1
        assert body["data"]["contradictions"][0]["state"] == "open"


class TestPrincipalsQuery:
    def test_requires_admin(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(client, "{ principals { id } }", headers=_auth(token))
        assert _error_codes(body) == ["CAPABILITY_ERROR"]

    def test_admin_lists_principals(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)
        body = _gql(client, "{ principals { id kind } }", headers=_auth(token))
        ids = {p["id"] for p in body["data"]["principals"]}
        assert HUMAN in ids
        assert ADMIN in ids


# ---------------------------------------------------------------------------
# Mutation.propose
# ---------------------------------------------------------------------------


class TestProposeMutation:
    _MUTATION = """
    mutation($input: ProposeInput!) {
      propose(input: $input) {
        decision
        proposal { id state author }
      }
    }
    """

    def test_literal_proposal(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(
            client,
            self._MUTATION,
            variables={
                "input": {
                    "subject": entity.id,
                    "predicate": "Person.name",
                    "value": "Ada",
                    "valueType": "Text",
                }
            },
            headers=_auth(token),
        )
        result = body["data"]["propose"]
        assert result["decision"] == "AutoAccept"
        assert result["proposal"]["state"] == "auto_accepted"
        assert result["proposal"]["author"] == HUMAN

    def test_ref_proposal(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        person = kb.create_entity("Person", author=HUMAN)
        org = kb.create_entity("Organization", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(
            client,
            self._MUTATION,
            variables={
                "input": {
                    "subject": person.id,
                    "predicate": "Person.employer",
                    "target": org.id,
                }
            },
            headers=_auth(token),
        )
        assert body["data"]["propose"]["proposal"]["state"] == "auto_accepted"

    def test_neither_value_nor_target_is_validation_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(
            client,
            self._MUTATION,
            variables={"input": {"subject": entity.id, "predicate": "Person.name"}},
            headers=_auth(token),
        )
        assert _error_codes(body) == ["VALIDATION_ERROR"]

    def test_both_value_and_target_is_validation_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        org = kb.create_entity("Organization", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(
            client,
            self._MUTATION,
            variables={
                "input": {
                    "subject": entity.id,
                    "predicate": "Person.name",
                    "value": "Ada",
                    "valueType": "Text",
                    "target": org.id,
                }
            },
            headers=_auth(token),
        )
        assert _error_codes(body) == ["VALIDATION_ERROR"]


# ---------------------------------------------------------------------------
# Mutation.acceptProposal / rejectProposal / requestChanges / resubmitProposal
# ---------------------------------------------------------------------------


class TestProposalReviewMutations:
    def _pending_proposal_id(self, kb: Ontology) -> str:
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _decision = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI, model="gpt-test"
        )
        return proposal.id

    def test_accept(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        proposal_id = self._pending_proposal_id(kb)
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        body = _gql(
            client,
            "mutation($id: String!) { acceptProposal(proposalId: $id) { state } }",
            variables={"id": proposal_id},
            headers=_auth(token),
        )
        assert body["data"]["acceptProposal"]["state"] == "accepted"

    def test_reject(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        proposal_id = self._pending_proposal_id(kb)
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        body = _gql(
            client,
            'mutation($id: String!) { rejectProposal(proposalId: $id, reason: "nope") { state } }',
            variables={"id": proposal_id},
            headers=_auth(token),
        )
        assert body["data"]["rejectProposal"]["state"] == "rejected"

    def test_request_changes_then_resubmit(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        proposal_id = self._pending_proposal_id(kb)
        client, _ = _client(kb)
        reviewer_token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        body = _gql(
            client,
            'mutation($id: String!) { requestChanges(proposalId: $id, reason: "fix it") { state } }',
            variables={"id": proposal_id},
            headers=_auth(reviewer_token),
        )
        assert body["data"]["requestChanges"]["state"] == "changes_requested"

        ai_token, _ = kb.issue_token(AI, author=ADMIN)
        body = _gql(
            client,
            "mutation($id: String!) { resubmitProposal(proposalId: $id) { proposal { state } decision } }",
            variables={"id": proposal_id},
            headers=_auth(ai_token),
        )
        assert body["data"]["resubmitProposal"]["proposal"]["state"] == "require_review"

    def test_self_review_is_blocked(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _decision = kb.propose(
            entity.id, "Person.name", "Ada", "Text", REVIEWER, source="human"
        )
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        body = _gql(
            client,
            "mutation($id: String!) { acceptProposal(proposalId: $id) { state } }",
            variables={"id": proposal.id},
            headers=_auth(token),
        )
        assert _error_codes(body) == ["CAPABILITY_ERROR"]


# ---------------------------------------------------------------------------
# Mutation.flagContradiction / resolveContradiction
# ---------------------------------------------------------------------------


class TestContradictionMutations:
    def test_flag_and_resolve(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        e = kb.create_entity("Person", author=HUMAN)
        a1 = kb.assert_literal(e.id, "Person.name", "Ada", "Text", author=HUMAN)
        a2 = kb.assert_literal(e.id, "Person.name", "Ida", "Text", author=ADMIN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)
        flag_query = """
        mutation($a: String!, $b: String!) {
          flagContradiction(assertionIdA: $a, assertionIdB: $b) {
            action
            contradiction { id state memberIds }
          }
        }
        """
        body = _gql(
            client,
            flag_query,
            variables={"a": a1.id, "b": a2.id},
            headers=_auth(token),
        )
        contradiction_id = body["data"]["flagContradiction"]["contradiction"]["id"]

        # Resolved by REVIEWER, not ADMIN: the resolver must not be a party
        # (author/delegate) to any member assertion (KI-026), and ADMIN
        # authored a2 above.
        reviewer_token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        resolve_query = """
        mutation($cid: String!, $winner: String!) {
          resolveContradiction(contradictionId: $cid, winnerAssertionId: $winner) {
            state
            resolvedBy
          }
        }
        """
        body = _gql(
            client,
            resolve_query,
            variables={"cid": contradiction_id, "winner": a1.id},
            headers=_auth(reviewer_token),
        )
        assert body["data"]["resolveContradiction"]["state"] == "resolved"
        assert body["data"]["resolveContradiction"]["resolvedBy"] == REVIEWER
