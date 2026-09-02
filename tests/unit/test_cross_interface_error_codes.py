"""Cross-interface error-code taxonomy regression (KI-059, SPEC §16).

REST (`ErrorOut(code=exc.code, ...)`) and GraphQL (`extensions = {"code":
original.code, ...}`) both pass an `OntolithError`'s own `.code` straight
through unmodified. MCP used to hand-write its own lowercase literals
(`"auth_error"` etc.) instead, diverging from both in casing and, for
not-found, in the literal string itself. These tests drive the *same*
underlying exception type through all three interfaces — sharing one
`Ontology`/`AuthProvider`, the way a real deployment exposes all three over
one backend — and assert they return the identical `code` string. A
regression here means the taxonomy has drifted again, exactly the class of
gap that motivated KI-059.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.identity.token_auth import TokenAuthProvider
from ontolith.interfaces.graphql import create_graphql_app
from ontolith.interfaces.mcp import create_mcp_server
from ontolith.interfaces.rest import create_rest_app

T0 = datetime(2025, 1, 1, tzinfo=UTC)
HUMAN = "alice@example.com"
ADMIN = "admin@example.com"


@pytest.fixture
def kb(tmp_path: Path) -> Ontology:
    """One Ontology shared by all three interfaces below — matches how a
    real deployment exposes REST/GraphQL/MCP over the same backend, and
    means a token issued once resolves identically against all three."""
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(50)])
    kb = Ontology.connect(tmp_path / "shared.db", clock=clock, id_provider=ids)
    kb.create_principal(HUMAN, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
    return kb


def _codes_for_missing_entity(
    kb: Ontology, headers: dict[str, str], token: str | None
) -> dict[str, str]:
    """Query the same nonexistent entity id through all three interfaces,
    returning each one's reported `code`."""
    auth_provider = TokenAuthProvider(kb.backend)

    rest_client = TestClient(create_rest_app(kb, auth_provider))
    rest_response = rest_client.get("/entities/does-not-exist", headers=headers)
    rest_code: str = rest_response.json()["code"]

    graphql_client = TestClient(create_graphql_app(kb, auth_provider))
    graphql_response = graphql_client.post(
        "/graphql",
        json={"query": '{ entity(id: "does-not-exist") { id } }'},
        headers=headers,
    )
    assert graphql_response.status_code == 200
    graphql_code: str = graphql_response.json()["errors"][0]["extensions"]["code"]

    mcp = create_mcp_server(kb, auth_provider)
    mcp_result = mcp._tool_manager.get_tool("ontolith.get").fn(
        entity_id="does-not-exist", token=token
    )
    mcp_code: str = mcp_result["code"]

    return {"rest": rest_code, "graphql": graphql_code, "mcp": mcp_code}


class TestAuthErrorCodeParity:
    """Same underlying exception type (AuthError, from a bad bearer token),
    triggered through the auth layer every one of the three interfaces
    shares, must produce the same code everywhere."""

    def test_all_three_interfaces_report_the_same_code(self, kb: Ontology) -> None:
        bad_headers = {"Authorization": "Bearer not-a-real-token"}

        codes = _codes_for_missing_entity(kb, bad_headers, token="not-a-real-token")

        assert codes == {"rest": "AUTH_ERROR", "graphql": "AUTH_ERROR", "mcp": "AUTH_ERROR"}


class TestNotFoundErrorCodeParity:
    """Same underlying exception type (NotFoundError, from a missing
    entity), triggered through each interface's own entity-lookup route —
    REST's GET /entities/{id} raises NotFoundError directly; MCP's
    ontolith.get checks for None and returns a matching code by hand
    (KI-059 also closed a gap here: this path previously had no code key
    at all, not just the wrong casing)."""

    def test_all_three_interfaces_report_the_same_code(self, kb: Ontology) -> None:
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        good_headers = {"Authorization": f"Bearer {token}"}

        codes = _codes_for_missing_entity(kb, good_headers, token=token)

        assert codes == {"rest": "NOT_FOUND", "graphql": "NOT_FOUND", "mcp": "NOT_FOUND"}


_FLAG_CONTRADICTION_MUTATION = """
mutation($a: String!, $b: String!) {
  flagContradiction(assertionIdA: $a, assertionIdB: $b) {
    action
  }
}
"""


class TestCapabilityErrorCodeParity:
    """Same underlying exception type (CapabilityError, from a read-only
    principal attempting flag_contradiction, an explicitly >= propose
    action per its own docstring), across all three interfaces — the
    other of the two error shapes MCP used to hand-write (alongside
    not-found) that the two classes above don't exercise."""

    def test_all_three_interfaces_report_the_same_code(self, kb: Ontology) -> None:
        entity = kb.create_entity("Person", author=HUMAN)
        assertion_a = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        assertion_b = kb.assert_literal(entity.id, "Person.name", "Ava", "Text", HUMAN)
        kb.create_principal(
            "readonly@example.com", kind="human", default_capability="read", author=ADMIN
        )
        token, _ = kb.issue_token("readonly@example.com", author=ADMIN)
        headers = {"Authorization": f"Bearer {token}"}
        auth_provider = TokenAuthProvider(kb.backend)

        rest_client = TestClient(create_rest_app(kb, auth_provider))
        rest_response = rest_client.post(
            "/contradictions/flag",
            json={"assertion_id_a": assertion_a.id, "assertion_id_b": assertion_b.id},
            headers=headers,
        )
        rest_code: str = rest_response.json()["code"]

        graphql_client = TestClient(create_graphql_app(kb, auth_provider))
        graphql_response = graphql_client.post(
            "/graphql",
            json={
                "query": _FLAG_CONTRADICTION_MUTATION,
                "variables": {"a": assertion_a.id, "b": assertion_b.id},
            },
            headers=headers,
        )
        assert graphql_response.status_code == 200
        graphql_code: str = graphql_response.json()["errors"][0]["extensions"]["code"]

        mcp = create_mcp_server(kb, auth_provider)
        mcp_result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=assertion_a.id, assertion_id_b=assertion_b.id, token=token
        )
        mcp_code: str = mcp_result["code"]

        assert rest_code == graphql_code == mcp_code == "CAPABILITY_ERROR"


class _FailingSchemaBackend:
    """Wraps a real StorageBackend, making ``get_schema`` raise a
    StorageError whose message interpolates fake internal detail — the
    shared vehicle below for triggering a genuine 5xx-class fault
    identically across all three interfaces (mirrors
    test_mcp_server.py's own copy of this fixture, but with a non-empty
    ``detail`` — unlike the message, `detail` is never redacted by any of
    the three interfaces, so an empty dict here couldn't distinguish
    "propagated correctly" from "an interface silently drops it")."""

    def __init__(self, real: object) -> None:
        self._real = real

    def get_schema(self, namespace: str) -> object:
        from ontolith.core.errors import StorageError

        raise StorageError(
            "sqlite3.OperationalError: database is locked (fd=7, pid=12345)",
            detail={"sqlite_errno": 5},
        )

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class TestStorageErrorRedactionParity:
    """Same underlying exception type (StorageError, a redacted 5xx-class
    fault), triggered through each interface's own GET-schema path, must
    be redacted identically everywhere: `code` matches, the raw internal
    message never reaches any of the three, and each still carries the
    generic message in its own message-shaped field. KI-074's review
    found MCP's blanket handler could regress this class of parity
    silently (see ADR-0014's KI-074 update, "review-driven follow-up") —
    this test exists so a future regression fails here instead."""

    def test_all_three_interfaces_redact_the_same_way(self, kb: Ontology) -> None:
        kb.backend = _FailingSchemaBackend(kb.backend)  # type: ignore[assignment]
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        headers = {"Authorization": f"Bearer {token}"}
        auth_provider = TokenAuthProvider(kb.backend)

        rest_client = TestClient(create_rest_app(kb, auth_provider))
        rest_response = rest_client.get("/schema", headers=headers)
        rest_body = rest_response.json()

        graphql_client = TestClient(create_graphql_app(kb, auth_provider))
        graphql_response = graphql_client.post(
            "/graphql",
            json={"query": "{ schema { concepts { name } } }"},
            headers=headers,
        )
        assert graphql_response.status_code == 200
        graphql_error = graphql_response.json()["errors"][0]

        mcp = create_mcp_server(kb, auth_provider)
        mcp_result = mcp._tool_manager.get_tool("ontolith.schema").fn(token=token)

        assert rest_response.status_code == 500
        assert (
            rest_body["code"]
            == graphql_error["extensions"]["code"]
            == mcp_result["code"]
            == "STORAGE_ERROR"
        )
        generic = "An internal error occurred"
        assert rest_body["message"] == graphql_error["message"] == mcp_result["error"] == generic
        # detail is never redacted (only the message is) — each interface
        # must still forward it unchanged, unlike the generic message
        for detail in (
            rest_body["detail"],
            graphql_error["extensions"]["detail"],
            mcp_result["detail"],
        ):
            assert detail == {"sqlite_errno": 5}
        assert "database is locked" not in rest_response.text
        assert "database is locked" not in graphql_response.text
