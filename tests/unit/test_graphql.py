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


class TestIntrospection:
    def test_disabled_by_default(self, tmp_path: Path) -> None:
        """KI-056: introspection defaults to off - depth/alias limiting
        can't bound it (graphql-core hardcodes an introspection carve-out
        in its own depth validator), so an unauthenticated recursive
        __schema query has no other mitigation in this factory."""
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        body = _gql(client, "{ __schema { queryType { name } } }")
        assert body["data"] is None
        assert "errors" in body

    def test_can_be_enabled_explicitly(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        auth_provider = TokenAuthProvider(kb.backend)
        app = create_graphql_app(kb, auth_provider, introspection=True)
        client = TestClient(app)
        body = _gql(client, "{ __schema { queryType { name } } }")
        assert "errors" not in body
        assert body["data"]["__schema"]["queryType"]["name"] == "Query"

    def test_disabled_independent_of_ide(self, tmp_path: Path) -> None:
        """graphql_ide has its own separate on/off switch - explicitly
        disabling introspection while leaving graphql_ide at its default
        must still block a raw POST from running __schema (review finding
        for the original, pre-KI-056 introspection=True default)."""
        kb = _kb(tmp_path)
        auth_provider = TokenAuthProvider(kb.backend)
        app = create_graphql_app(kb, auth_provider, introspection=False)
        client = TestClient(app)
        body = _gql(client, "{ __schema { queryType { name } } }")
        assert body["data"] is None
        assert "errors" in body


class TestQueryAmplificationLimits:
    """KI-056: MaxAliasesLimiter/QueryDepthLimiter are always on (not tied
    to the introspection flag) - bound the alias-amplification vector
    KI-052's async conversion made cross-interface (shared anyio thread
    pool with any co-mounted REST app). max_alias_count=15 doesn't
    eliminate that vector, only reduces it (15 concurrent worker threads
    per request against a shared pool of 40) - see ADR-0037's Update for
    the full accounting; this class only pins the limiter's own behavior."""

    @staticmethod
    def _alias_query(count: int) -> str:
        return "{ " + " ".join(f"a{i}: schema {{ version }}" for i in range(count)) + " }"

    def test_at_alias_limit_succeeds(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(client, self._alias_query(15), headers=_auth(token))
        assert "errors" not in body

    def test_one_over_alias_limit_rejected(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(client, self._alias_query(16), headers=_auth(token))
        assert body["data"] is None
        assert "aliases found" in body["errors"][0]["message"]

    def test_alias_limit_still_applies_when_introspection_is_enabled(self, tmp_path: Path) -> None:
        """The limiters must not be nested inside the `if not
        introspection:` branch - review finding: a version that was would
        still pass every other test in this class, since none of them
        exercise introspection=True."""
        kb = _kb(tmp_path)
        auth_provider = TokenAuthProvider(kb.backend)
        app = create_graphql_app(kb, auth_provider, introspection=True)
        client = TestClient(app)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(client, self._alias_query(16), headers=_auth(token))
        assert body["data"] is None
        assert "aliases found" in body["errors"][0]["message"]

    def test_excess_depth_rejected(self, tmp_path: Path) -> None:
        """No field in this schema is recursive, so no schema-valid query
        can actually reach 10 levels of nesting - this probe query is
        deliberately invalid (PropertyType has no `properties` field) to
        force enough AST depth to trip the limiter; the response also
        carries a field-validity error alongside the depth one, so this
        asserts membership rather than errors[0] specifically."""
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        nested = "name"
        for _ in range(12):
            nested = f"properties {{ {nested} }}"
        query = f"{{ schema {{ concepts {{ {nested} }} }} }}"
        body = _gql(client, query, headers=_auth(token))
        assert body["data"] is None
        assert any("exceeds maximum operation depth" in e["message"] for e in body["errors"])


class TestDocsUrls:
    def test_docs_enabled_by_default(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200

    def test_docs_can_be_disabled(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        auth_provider = TokenAuthProvider(kb.backend)
        app = create_graphql_app(kb, auth_provider, docs_url=None, redoc_url=None, openapi_url=None)
        client = TestClient(app)
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


# ---------------------------------------------------------------------------
# Concurrency (KI-052): resolvers must not block the ASGI event loop.
# Uses httpx2's ASGITransport + asyncio.gather, not FastAPI's TestClient -
# TestClient runs the app through a single background portal thread, which
# doesn't exercise real concurrent event-loop scheduling the way a live
# server would; a genuine async client talking to the app in-process does.
# ---------------------------------------------------------------------------


async def _post_concurrently(
    app: object, headers: dict[str, str], query: str, count: int
) -> tuple[list[int], list[dict[str, Any]], float]:
    """Fire `count` concurrent GraphQL requests through a real ASGI
    transport (not FastAPI's TestClient - see TestResolverConcurrency's own
    docstring) and report status codes, JSON bodies, and elapsed time."""
    import asyncio
    import time

    import httpx2

    transport = httpx2.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        start = time.perf_counter()
        responses = await asyncio.wait_for(
            asyncio.gather(
                *[
                    client.post("/graphql", json={"query": query}, headers=headers)
                    for _ in range(count)
                ]
            ),
            timeout=15,
        )
        elapsed = time.perf_counter() - start
        return [r.status_code for r in responses], [r.json() for r in responses], elapsed


class TestResolverConcurrency:
    def test_concurrent_requests_overlap_instead_of_serializing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Before KI-052, every resolver ran inline on the event loop, so
        three concurrent 0.3s-resolver requests took ~0.9s (serialized).
        After converting resolvers to async + run_in_threadpool, they
        overlap - this must take closer to 0.3s than 0.9s.

        Asserts the slowed path actually ran (not just that responses came
        back 200) - a resolver that stopped reaching get_schema entirely
        (e.g. a future schema cache) would otherwise still return 200 fast
        and pass vacuously without exercising the offload at all."""
        import asyncio
        import time

        kb = _kb(tmp_path)
        auth_provider = TokenAuthProvider(kb.backend)
        app = create_graphql_app(kb, auth_provider)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        headers = _auth(token)

        original_get_schema = kb.backend.get_schema
        call_count = 0

        def _slow_get_schema(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            time.sleep(0.3)
            return original_get_schema(*args, **kwargs)

        monkeypatch.setattr(kb.backend, "get_schema", _slow_get_schema)

        statuses, bodies, elapsed = asyncio.run(
            _post_concurrently(app, headers, "{ schema { version } }", 3)
        )
        assert statuses == [200, 200, 200]
        assert all("errors" not in b for b in bodies), bodies
        assert all(b["data"]["schema"] is not None for b in bodies), bodies
        assert call_count == 3
        # Serialized would be ~0.9s (3 x 0.3s); overlapping is ~0.3s. 0.6s
        # is a generous midpoint that tolerates test-machine jitter while
        # still failing if resolvers regress to blocking the event loop.
        assert elapsed < 0.6, f"expected overlapping concurrent requests, took {elapsed:.2f}s"

    def test_context_auth_resolution_does_not_block_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create_graphql_app's _get_context offloads auth_provider.resolve()
        too, not just the resolver body - a regression here would re-block
        the event loop on *every* authenticated request, not just one
        field, since every request resolves its token first."""
        import asyncio
        import time

        kb = _kb(tmp_path)
        auth_provider = TokenAuthProvider(kb.backend)
        app = create_graphql_app(kb, auth_provider)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        headers = _auth(token)

        original_resolve = auth_provider.resolve
        call_count = 0

        def _slow_resolve(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            time.sleep(0.3)
            return original_resolve(*args, **kwargs)

        monkeypatch.setattr(auth_provider, "resolve", _slow_resolve)

        statuses, bodies, elapsed = asyncio.run(
            _post_concurrently(app, headers, "{ schema { version } }", 3)
        )
        assert statuses == [200, 200, 200]
        assert all("errors" not in b for b in bodies), bodies
        assert all(b["data"]["schema"] is not None for b in bodies), bodies
        assert call_count == 3
        assert elapsed < 0.6, f"expected overlapping concurrent requests, took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuth:
    def test_introspection_works_unauthenticated_when_enabled(self, tmp_path: Path) -> None:
        """Auth deferral (module docstring) is about context_getter never
        raising, so introspection - when explicitly enabled, see
        TestIntrospection for the KI-056 default-off behavior - stays
        reachable without a token."""
        kb = _kb(tmp_path)
        auth_provider = TokenAuthProvider(kb.backend)
        app = create_graphql_app(kb, auth_provider, introspection=True)
        client = TestClient(app)
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

    def test_rejects_missing_bearer_prefix(self, tmp_path: Path) -> None:
        """A raw token with no `Bearer ` prefix must not be accepted -
        pins the exact scheme this module's docstring promises."""
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(client, "{ schema { version } }", headers={"Authorization": token})
        assert _error_codes(body) == ["AUTH_ERROR"]

    def test_rejects_lowercase_bearer_scheme(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(client, "{ schema { version } }", headers={"Authorization": f"bearer {token}"})
        assert _error_codes(body) == ["AUTH_ERROR"]

    def test_storage_error_subclass_is_also_redacted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_REDACT_MESSAGE_FOR uses isinstance, not exact type, so a future
        StorageError subclass still gets redacted - pins the fail-closed
        behavior directly rather than trusting the isinstance change by
        reading it."""
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)

        class _SubStorageError(StorageError):
            pass

        def _raise_sub_storage_error(*args: object, **kwargs: object) -> None:
            raise _SubStorageError("sqlite3: some internal detail")

        monkeypatch.setattr(kb.backend, "get_schema", _raise_sub_storage_error)

        body = _gql(client, "{ schema { version } }", headers=_auth(token))
        assert _error_codes(body) == ["STORAGE_ERROR"]
        message = body["errors"][0]["message"]
        assert message == "An internal error occurred"
        assert "sqlite3" not in message

    def test_storage_error_from_token_resolution_is_redacted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A StorageError raised by auth_provider.resolve() itself (not a
        resolver) must still flow through the same redaction path, not
        escape as a raw, unmapped 500 (review finding)."""
        kb = _kb(tmp_path)
        client, auth_provider = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)

        def _raise_storage_error(*args: object, **kwargs: object) -> None:
            raise StorageError("sqlite3: token lookup failed")

        monkeypatch.setattr(auth_provider, "resolve", _raise_storage_error)

        body = _gql(client, "{ schema { version } }", headers=_auth(token))
        assert _error_codes(body) == ["STORAGE_ERROR"]
        message = body["errors"][0]["message"]
        assert message == "An internal error occurred"
        assert "sqlite3" not in message

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

    def test_non_ontolith_exception_is_redacted_like_storage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolver raising a plain (non-OntolithError) exception - e.g. a
        real bug - must fail closed, same as REST's default 500 for the
        same failure class: the real message never reaches the client, only
        a generic message and a distinct INTERNAL_ERROR code (review
        finding - the raw message previously leaked, unlike REST)."""
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)

        def _raise_runtime_error(*args: object, **kwargs: object) -> None:
            raise RuntimeError("boom: /var/db/secret_path")

        monkeypatch.setattr(kb.backend, "get_schema", _raise_runtime_error)

        body = _gql(client, "{ schema { version } }", headers=_auth(token))
        assert _error_codes(body) == ["INTERNAL_ERROR"]
        message = body["errors"][0]["message"]
        assert message == "An internal error occurred"
        assert "boom" not in message
        assert "secret_path" not in str(body)

    def test_plugin_error_redacts_internal_detail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ontolith.core.errors import PluginError

        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)

        def _raise_plugin_error(*args: object, **kwargs: object) -> None:
            raise PluginError("plugin 'foo' crashed: Traceback (most recent call last)...")

        monkeypatch.setattr(kb.backend, "get_schema", _raise_plugin_error)

        body = _gql(client, "{ schema { version } }", headers=_auth(token))
        assert _error_codes(body) == ["PLUGIN_ERROR"]
        message = body["errors"][0]["message"]
        assert message == "An internal error occurred"
        assert "Traceback" not in message

    def test_graphql_parse_error_is_not_redacted(self, tmp_path: Path) -> None:
        """A malformed query never reaches a resolver (original_error is
        None) - that failure describes the query's own shape, not server
        internals, so it must NOT be swept into the generic redaction."""
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        body = _gql(client, "{ schema { ")
        assert "errors" in body
        assert body["errors"][0]["message"] != "An internal error occurred"


# ---------------------------------------------------------------------------
# Every Query/Mutation field requires auth (review finding: this was
# previously exercised for `schema` only - mutation testing showed deleting
# _require_principal from `entity`/`query`/`provenance`/`proposals`/
# `contradictions` left the suite green).
# ---------------------------------------------------------------------------


class TestAuthCoversEveryField:
    # One syntactically-valid probe query per root field, using dummy
    # argument values - _require_principal(info) raises before any argument
    # is ever used, so an unauthenticated call never reaches real data.
    # The `test_*_field_probe_set_matches_schema` tests below assert this
    # dict's keys are exactly the schema's field set, so a newly-added field
    # with no matching probe entry fails loudly instead of silently going
    # unchecked.
    _QUERY_FIELD_PROBES: dict[str, str] = {
        "schema": "{ schema { version } }",
        "entity": '{ entity(id: "nope") { id } }',
        "query": '{ query(concept: "Person") { count } }',
        "provenance": '{ provenance(assertionId: "nope") { id } }',
        "proposals": "{ proposals { id } }",
        "contradictions": "{ contradictions { id } }",
        "principals": "{ principals { id } }",
    }
    _MUTATION_FIELD_PROBES: dict[str, str] = {
        "propose": (
            'mutation { propose(input: {subject: "s", predicate: "p", value: "v", '
            'valueType: "Text"}) { decision } }'
        ),
        "acceptProposal": 'mutation { acceptProposal(proposalId: "nope") { id } }',
        "rejectProposal": 'mutation { rejectProposal(proposalId: "nope") { id } }',
        "requestChanges": 'mutation { requestChanges(proposalId: "nope") { id } }',
        "resubmitProposal": 'mutation { resubmitProposal(proposalId: "nope") { decision } }',
        "retract": 'mutation { retract(assertionId: "nope") { decision } }',
        "flagContradiction": (
            'mutation { flagContradiction(assertionIdA: "a", assertionIdB: "b") { action } }'
        ),
        "resolveContradiction": (
            'mutation { resolveContradiction(contradictionId: "c", '
            'winnerAssertionId: "w") { state } }'
        ),
    }

    def test_query_field_probe_set_matches_schema(self, tmp_path: Path) -> None:
        # Introspection is off by default (KI-056) - needs explicit opt-in
        # here purely to enumerate the schema's own field list for this
        # test; unrelated to the auth-gating behavior under test elsewhere.
        kb = _kb(tmp_path)
        app = create_graphql_app(kb, TokenAuthProvider(kb.backend), introspection=True)
        client = TestClient(app)
        body = _gql(client, '{ __type(name: "Query") { fields { name } } }')
        names = {f["name"] for f in body["data"]["__type"]["fields"]}
        assert names == set(self._QUERY_FIELD_PROBES)

    def test_mutation_field_probe_set_matches_schema(self, tmp_path: Path) -> None:
        """Also enforces ADR-0037 §1's scope boundary: this must be exactly
        the eight query/propose/review/retract operations (retract added
        for KI-057, ADR-0039), no direct-write or principal-admin mutation
        (mirrors test_mcp_server.py's test_no_write_tool_registered
        precedent)."""
        kb = _kb(tmp_path)
        app = create_graphql_app(kb, TokenAuthProvider(kb.backend), introspection=True)
        client = TestClient(app)
        body = _gql(client, '{ __type(name: "Mutation") { fields { name } } }')
        names = {f["name"] for f in body["data"]["__type"]["fields"]}
        assert names == set(self._MUTATION_FIELD_PROBES)

    @pytest.mark.parametrize("field_name,query", list(_QUERY_FIELD_PROBES.items()))
    def test_query_field_requires_auth(self, tmp_path: Path, field_name: str, query: str) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        body = _gql(client, query)
        assert _error_codes(body) == ["AUTH_ERROR"], f"Query.{field_name} did not require auth"
        assert body["data"] is None

    @pytest.mark.parametrize("field_name,query", list(_MUTATION_FIELD_PROBES.items()))
    def test_mutation_field_requires_auth(
        self, tmp_path: Path, field_name: str, query: str
    ) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        body = _gql(client, query)
        assert _error_codes(body) == ["AUTH_ERROR"], f"Mutation.{field_name} did not require auth"
        assert body["data"] is None


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

    def test_assertions_field_requires_auth_on_its_own(self, tmp_path: Path) -> None:
        """EntityType.assertions calls _require_principal itself (review
        finding) rather than relying solely on Query.entity's own gate.
        Query.entity is the only path that currently reaches EntityType, and
        it always gates first, so no query-level probe can exercise this
        resolver unauthenticated - verified directly instead, invoking the
        strawberry-wrapped (async, KI-052) method with a minimal stub Info."""
        import asyncio

        from ontolith.core.errors import AuthError
        from ontolith.interfaces.graphql import EntityType

        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        class _StubInfo:
            context = {"kb": kb, "auth_error": AuthError("Missing or malformed header")}

        graphql_entity = EntityType(
            id=entity.id,
            concept="Person",
            namespace="default",
            natural_key=None,
            created_at="2025-01-01T00:00:00",
            created_by=HUMAN,
        )
        with pytest.raises(AuthError):
            asyncio.run(graphql_entity.assertions(_StubInfo()))  # type: ignore[arg-type]


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

    def test_duplicate_filter_keys_is_validation_error(self, tmp_path: Path) -> None:
        """A [FilterInput!] list can express what REST's plain dict body
        never could - the same key twice. Silently keeping the last one
        (dict-comprehension last-wins) would drop a filter with no error;
        must fail loudly instead (review finding)."""
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        query = """
        query($filters: [FilterInput!]) {
          query(concept: "Person", filters: $filters) { count }
        }
        """
        body = _gql(
            client,
            query,
            variables={
                "filters": [
                    {"key": "name", "value": "Ada"},
                    {"key": "name", "value": "Bob"},
                ]
            },
            headers=_auth(token),
        )
        assert _error_codes(body) == ["VALIDATION_ERROR"]

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

    def test_null_returns_every_state(self, tmp_path: Path) -> None:
        """An explicit JSON `null` works the same as `state: "all"` - unlike
        REST, GraphQL doesn't strictly need the string sentinel (a `null`
        variable is unambiguous against an omitted one), but both are
        accepted (KI-077 review)."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)  # auto-accepted

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(
            client,
            "query($s: String) { proposals(state: $s) { id state } }",
            variables={"s": None},
            headers=_auth(token),
        )
        assert len(body["data"]["proposals"]) == 1
        assert body["data"]["proposals"][0]["state"] == "auto_accepted"

    def test_pending_merges_changes_requested(self, tmp_path: Path) -> None:
        """KI-027: request_changes() moves a proposal out of require_review
        - state="pending" is an explicit, documented alias merging both
        require_review and changes_requested (mirrors REST's own test)."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="gpt-test")
        kb.request_changes(proposal.id, REVIEWER)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(client, '{ proposals(state: "pending") { id state } }', headers=_auth(token))
        assert len(body["data"]["proposals"]) == 1
        assert body["data"]["proposals"][0]["state"] == "changes_requested"

    def test_unrecognized_state_returns_validation_error(self, tmp_path: Path) -> None:
        """KI-077: an unrecognized `state` value previously reached
        kb.proposals()'s own WHERE state = ? unfiltered and silently
        matched zero rows - indistinguishable from "no proposals in that
        state." Must raise instead."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        for bad_state in ("Auto_accepted", "pendng", "All", "", "None", "open"):
            body = _gql(
                client,
                "query($s: String) { proposals(state: $s) { id } }",
                variables={"s": bad_state},
                headers=_auth(token),
            )
            assert _error_codes(body) == ["VALIDATION_ERROR"], bad_state


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

    def test_state_resolved_filters_correctly(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        e = kb.create_entity("Person", author=HUMAN)
        a1 = kb.assert_literal(e.id, "Person.name", "Ada", "Text", author=HUMAN)
        kb.assert_literal(e.id, "Person.name", "Ida", "Text", author=ADMIN)
        [contradiction] = kb.contradictions()

        client, _ = _client(kb)
        reviewer_token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        _gql(
            client,
            "mutation($cid: String!, $w: String!) { "
            "resolveContradiction(contradictionId: $cid, winnerAssertionId: $w) { state } }",
            variables={"cid": contradiction.id, "w": a1.id},
            headers=_auth(reviewer_token),
        )

        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(
            client, '{ contradictions(state: "resolved") { id state } }', headers=_auth(token)
        )
        assert len(body["data"]["contradictions"]) == 1
        assert body["data"]["contradictions"][0]["state"] == "resolved"

    def test_all_sentinel_returns_every_state(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        e = kb.create_entity("Person", author=HUMAN)
        a1 = kb.assert_literal(e.id, "Person.name", "Ada", "Text", author=HUMAN)
        kb.assert_literal(e.id, "Person.name", "Ida", "Text", author=ADMIN)
        [contradiction] = kb.contradictions()

        client, _ = _client(kb)
        reviewer_token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        _gql(
            client,
            "mutation($cid: String!, $w: String!) { "
            "resolveContradiction(contradictionId: $cid, winnerAssertionId: $w) { state } }",
            variables={"cid": contradiction.id, "w": a1.id},
            headers=_auth(reviewer_token),
        )

        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(client, '{ contradictions(state: "all") { id state } }', headers=_auth(token))
        assert len(body["data"]["contradictions"]) == 1
        assert body["data"]["contradictions"][0]["state"] == "resolved"

    def test_null_returns_every_state(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        e = kb.create_entity("Person", author=HUMAN)
        kb.assert_literal(e.id, "Person.name", "Ada", "Text", author=HUMAN)
        kb.assert_literal(e.id, "Person.name", "Ida", "Text", author=ADMIN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        body = _gql(
            client,
            "query($s: String) { contradictions(state: $s) { id state } }",
            variables={"s": None},
            headers=_auth(token),
        )
        assert len(body["data"]["contradictions"]) == 1
        assert body["data"]["contradictions"][0]["state"] == "open"

    def test_unrecognized_state_returns_validation_error(self, tmp_path: Path) -> None:
        """KI-077: an unrecognized `state` value previously reached
        kb.contradictions()'s own WHERE state = ? unfiltered and silently
        matched zero rows - indistinguishable from "no contradictions in
        that state." Must raise instead (same class of bug KI-076 fixed
        for MCP's ontolith.list_contradictions)."""
        kb = _kb(tmp_path)
        e = kb.create_entity("Person", author=HUMAN)
        kb.assert_literal(e.id, "Person.name", "Ada", "Text", author=HUMAN)
        kb.assert_literal(e.id, "Person.name", "Ida", "Text", author=ADMIN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        for bad_state in ("Open", "unresolved", "All", "", "None", "accepted"):
            body = _gql(
                client,
                "query($s: String) { contradictions(state: $s) { id } }",
                variables={"s": bad_state},
                headers=_auth(token),
            )
            assert _error_codes(body) == ["VALIDATION_ERROR"], bad_state


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
# Mutation.retract (KI-057, ADR-0039)
# ---------------------------------------------------------------------------


class TestRetractMutation:
    def test_write_capability_auto_accepts(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)

        body = _gql(
            client,
            "mutation($id: String!) { retract(assertionId: $id) { proposal { state } decision } }",
            variables={"id": assertion.id},
            headers=_auth(token),
        )

        assert body["data"]["retract"]["proposal"]["state"] == "auto_accepted"
        assert body["data"]["retract"]["decision"] == "AutoAccept"
        retracted = kb.assertions(subject=entity.id, status=None)[0]
        assert retracted.status == "retracted"

    def test_ai_requires_review(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(AI, author=ADMIN)

        body = _gql(
            client,
            "mutation($id: String!) { retract(assertionId: $id) { proposal { state } decision } }",
            variables={"id": assertion.id},
            headers=_auth(token),
        )

        assert body["data"]["retract"]["proposal"]["state"] == "require_review"
        assert body["data"]["retract"]["decision"] == "RequireReview"

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
        client, _ = _client(kb)
        token, _ = kb.issue_token("delegate@example.com", author=ADMIN)

        body = _gql(
            client,
            "mutation($id: String!, $actingAs: String!) { "
            "retract(assertionId: $id, actingAs: $actingAs) "
            "{ proposal { actingAs } } }",
            variables={"id": assertion.id, "actingAs": HUMAN},
            headers=_auth(token),
        )

        assert body["data"]["retract"]["proposal"]["actingAs"] == HUMAN


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

    def test_rationale_is_readable_back_in_rationale_history(self, tmp_path: Path) -> None:
        """KI-075: `rationale` (KI-071) must round-trip through this
        mutation/query, not just be recorded server-side and unreachable."""
        kb = _kb(tmp_path)
        e = kb.create_entity("Person", author=HUMAN)
        a1 = kb.assert_literal(e.id, "Person.name", "Ada", "Text", author=HUMAN)
        a2 = kb.assert_literal(e.id, "Person.name", "Ida", "Text", author=ADMIN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)
        flag_query = """
        mutation($a: String!, $b: String!, $r: String!) {
          flagContradiction(assertionIdA: $a, assertionIdB: $b, rationale: $r) {
            contradiction {
              id
              rationaleHistory { rationale actor at }
            }
          }
        }
        """
        body = _gql(
            client,
            flag_query,
            variables={"a": a1.id, "b": a2.id, "r": "Sources disagree"},
            headers=_auth(token),
        )
        contradiction = body["data"]["flagContradiction"]["contradiction"]
        assert contradiction["rationaleHistory"] == [
            {"rationale": "Sources disagree", "actor": ADMIN, "at": T0.isoformat()}
        ]

        # Query.contradictions surfaces the same history for the same
        # contradiction, not just the mutation's own response.
        list_query = """
        query { contradictions { id rationaleHistory { rationale actor at } } }
        """
        body = _gql(client, list_query, headers=_auth(token))
        (listed,) = [c for c in body["data"]["contradictions"] if c["id"] == contradiction["id"]]
        assert listed["rationaleHistory"] == contradiction["rationaleHistory"]

        # KI-075 review: extend with a second rationale, pinning that this
        # response is read back from the persisted, re-fetched Contradiction
        # (post-write) rather than synthesized from just this call's own
        # `rationale` argument — a single-call test can't distinguish the two.
        a3 = kb.assert_literal(e.id, "Person.name", "Ida L.", "Text", author=ADMIN)
        kb.clock.advance(days=1)  # type: ignore[attr-defined]
        body = _gql(
            client,
            flag_query,
            variables={"a": a1.id, "b": a3.id, "r": "A third source also disagrees"},
            headers=_auth(token),
        )
        extended = body["data"]["flagContradiction"]["contradiction"]
        assert extended["rationaleHistory"] == [
            {"rationale": "Sources disagree", "actor": ADMIN, "at": T0.isoformat()},
            {
                "rationale": "A third source also disagrees",
                "actor": ADMIN,
                "at": "2025-01-02T00:00:00+00:00",
            },
        ]

        # And the accumulated trail survives resolveContradiction — the one
        # backend write (state/resolved_by/resolved_at only) that could
        # silently wipe `metadata` if it ever touched the wrong column.
        # REVIEWER, not ADMIN: the resolver must not be a party (author) to
        # any member assertion (KI-026), and ADMIN authored a2/a3 above.
        reviewer_token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        resolve_query = """
        mutation($cid: String!, $winner: String!) {
          resolveContradiction(contradictionId: $cid, winnerAssertionId: $winner) {
            rationaleHistory { rationale actor at }
          }
        }
        """
        body = _gql(
            client,
            resolve_query,
            variables={"cid": contradiction["id"], "winner": a1.id},
            headers=_auth(reviewer_token),
        )
        assert (
            body["data"]["resolveContradiction"]["rationaleHistory"] == extended["rationaleHistory"]
        )
