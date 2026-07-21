"""REST interface for Ontolith (SPEC §14.3, ADR-0021).

Exposes a read + propose slice over HTTP: schema, entity retrieval, query,
provenance, and proposal creation/listing. Direct write, proposal
accept/reject/review, contradiction resolution, and principal/token admin
are deferred to a follow-up PR (KI-022).

Authentication (ADR-0014, reused unchanged): every route requires an
``Authorization: Bearer <token>`` header, resolved server-side via the
injected AuthProvider — never a caller-asserted principal ID. This includes
read routes, a deliberate divergence from the *shipped* MCP server's
unauthenticated read tools (KI-021 tracks closing that gap on the MCP
side).

Usage:
    from ontolith.identity.token_auth import TokenAuthProvider
    from ontolith.interfaces.rest import create_rest_app
    app = create_rest_app(kb, TokenAuthProvider(kb.backend))
    # uvicorn.run(app) to serve
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ontolith.core.errors import (
    AuthError,
    CapabilityError,
    ConflictError,
    NotFoundError,
    OntolithError,
    PluginError,
    PolicyDenied,
    SchemaError,
    StorageError,
    ValidationError,
)
from ontolith.identity import Principal

if TYPE_CHECKING:
    from ontolith.identity.ports import AuthProvider
    from ontolith.ontology import Ontology

_STATUS_BY_ERROR_TYPE: dict[type[OntolithError], int] = {
    ValidationError: 400,
    SchemaError: 400,
    AuthError: 401,
    CapabilityError: 403,
    PolicyDenied: 403,
    ConflictError: 409,
    NotFoundError: 404,
    StorageError: 500,
    PluginError: 500,
}


# ---------------------------------------------------------------------------
# Response/request schemas
# ---------------------------------------------------------------------------


class PropertyOut(BaseModel):
    """A single concept property as returned by GET /schema."""

    name: str
    type: str
    temporality: str
    required: bool


class ConceptOut(BaseModel):
    """A single concept and its properties, as returned by GET /schema."""

    name: str
    properties: list[PropertyOut]


class SchemaOut(BaseModel):
    """Response body for GET /schema."""

    namespace: str | None = None
    version: int | None = None
    concepts: list[ConceptOut]


class ErrorOut(BaseModel):
    """Response body for every OntolithError (SPEC §16)."""

    code: str
    message: str
    detail: dict[str, Any]


class AssertionOut(BaseModel):
    """A single active assertion, as returned nested under an entity."""

    id: str
    predicate: str
    value: str
    value_type: str | None
    confidence: float | None
    author: str
    asserted_at: str


class EntityOut(BaseModel):
    """An entity's own fields, without its assertions."""

    id: str
    concept: str
    namespace: str
    natural_key: str | None
    created_at: str
    created_by: str


class EntityDetailOut(BaseModel):
    """Response body for GET /entities/{entity_id}."""

    entity: EntityOut
    assertions: list[AssertionOut]


class EntitySummaryOut(BaseModel):
    """A single entity's summary fields, as returned in a query result list."""

    id: str
    concept: str
    natural_key: str | None
    created_at: str


class QueryIn(BaseModel):
    """Request body for POST /query.

    No ``namespace`` field: ``Ontology.namespace`` is hardcoded to
    ``"default"`` (M1 limitation), so ``kb.query()`` has no namespace
    argument to forward one to.
    """

    concept: str
    filters: dict[str, str] | None = None
    semantic: str | None = None
    min_confidence: float | None = None
    trust_at_least: int | None = None
    limit: int | None = None


class QueryOut(BaseModel):
    """Response body for POST /query."""

    concept: str
    count: int
    entities: list[EntitySummaryOut]


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_rest_app(kb: Ontology, auth_provider: AuthProvider, name: str = "ontolith") -> FastAPI:
    """Build and return a FastAPI app bound to the given knowledge base.

    Args:
        kb: Ontology instance (knowledge base) to expose
        auth_provider: Resolves caller-supplied bearer tokens to Principals
            (ADR-0014) — e.g. ``TokenAuthProvider(kb.backend)``
        name: API title advertised in the OpenAPI schema

    Returns:
        Configured FastAPI app with the read + propose route slice
        (ADR-0021)
    """
    app = FastAPI(title=name)

    def _resolve_principal(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Principal:
        """Resolve the Authorization header to a Principal (ADR-0014).

        Raises:
            AuthError: header missing/malformed, or token invalid/revoked
        """
        if authorization is None or not authorization.startswith("Bearer "):
            raise AuthError("Missing or malformed Authorization header")
        token = authorization.removeprefix("Bearer ")
        return auth_provider.resolve(token)

    @app.exception_handler(OntolithError)
    def _handle_ontolith_error(_request: Request, exc: OntolithError) -> JSONResponse:
        """Map every OntolithError subtype to its HTTP status (SPEC §16)."""
        status = _STATUS_BY_ERROR_TYPE.get(type(exc), 500)
        body = ErrorOut(code=exc.code, message=exc.message, detail=exc.detail)
        return JSONResponse(status_code=status, content=body.model_dump())

    # ------------------------------------------------------------------
    # GET /schema
    # ------------------------------------------------------------------

    @app.get("/schema")
    def get_schema(
        namespace: str = "default",
        _principal: Principal = Depends(_resolve_principal),
    ) -> SchemaOut:
        """Return the schema (concepts and their properties) for a namespace."""
        ir = kb.backend.get_schema(namespace)
        if ir is None:
            return SchemaOut(concepts=[])
        concepts = [
            ConceptOut(
                name=concept_name,
                properties=[
                    PropertyOut(
                        name=prop_name,
                        type=prop_def.value_type,
                        temporality=prop_def.temporality,
                        required=prop_def.required,
                    )
                    for prop_name, prop_def in concept_def.properties.items()
                ],
            )
            for concept_name, concept_def in ir.concepts.items()
        ]
        return SchemaOut(namespace=namespace, version=ir.version, concepts=concepts)

    # ------------------------------------------------------------------
    # GET /entities/{entity_id}
    # ------------------------------------------------------------------

    @app.get("/entities/{entity_id}")
    def get_entity_route(
        entity_id: str,
        _principal: Principal = Depends(_resolve_principal),
    ) -> EntityDetailOut:
        """Fetch an entity and its currently active assertions."""
        entity = kb.backend.get_entity(entity_id)
        if entity is None:
            raise NotFoundError(f"Entity {entity_id!r} not found")
        assertions = kb.backend.assertions(subject=entity_id, status="active")
        return EntityDetailOut(
            entity=EntityOut(
                id=entity.id,
                concept=entity.concept,
                namespace=entity.namespace,
                natural_key=entity.natural_key,
                created_at=entity.created_at.isoformat(),
                created_by=entity.created_by,
            ),
            assertions=[
                AssertionOut(
                    id=a.id,
                    predicate=a.predicate,
                    value=a.value,
                    value_type=a.value_type,
                    confidence=a.confidence,
                    author=a.author,
                    asserted_at=a.asserted_at.isoformat(),
                )
                for a in assertions
            ],
        )

    # ------------------------------------------------------------------
    # POST /query
    # ------------------------------------------------------------------

    @app.post("/query")
    def query_route(
        body: QueryIn,
        _principal: Principal = Depends(_resolve_principal),
    ) -> QueryOut:
        """Query entities of a concept, optionally filtered/ranked."""
        builder = kb.query(body.concept)
        if body.filters:
            builder = builder.where(**body.filters)
        if body.semantic is not None:
            builder = builder.semantic(body.semantic)
        if body.min_confidence is not None:
            builder = builder.min_confidence(body.min_confidence)
        if body.trust_at_least is not None:
            builder = builder.trust_at_least(body.trust_at_least)
        if body.limit is not None:
            builder = builder.limit(body.limit)
        entities = builder.all()
        return QueryOut(
            concept=body.concept,
            count=len(entities),
            entities=[
                EntitySummaryOut(
                    id=e.id,
                    concept=e.concept,
                    natural_key=e.natural_key,
                    created_at=e.created_at.isoformat(),
                )
                for e in entities
            ],
        )

    return app


__all__ = ["create_rest_app"]
