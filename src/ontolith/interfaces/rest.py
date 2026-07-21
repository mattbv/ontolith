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


class ReviewEventOut(BaseModel):
    """A single review-decision event in an assertion's provenance trail."""

    actor: str
    type: str
    detail: str | None
    at: str


class ProvenanceOut(BaseModel):
    """Response body for GET /provenance/{assertion_id}."""

    id: str
    subject: str
    predicate: str
    value: str
    value_type: str | None
    status: str
    author: str
    confidence: float | None
    source: str | None
    rationale: str | None
    model: str | None
    asserted_at: str
    valid_from: str | None
    valid_to: str | None
    proposal_id: str | None
    supersedes: str | None
    review_events: list[ReviewEventOut]


class ProposeIn(BaseModel):
    """Request body for POST /proposals.

    Exactly one of (``value`` and ``value_type``) or ``target`` must be
    set — a literal assertion or a relation, never both, never neither.
    """

    subject: str
    predicate: str
    value: str | None = None
    value_type: str | None = None
    target: str | None = None
    confidence: float | None = None
    source: str | None = None
    rationale: str | None = None
    acting_as: str | None = None
    model: str | None = None


class ProposalOut(BaseModel):
    """A proposal's summary fields, returned by both proposal routes."""

    id: str
    namespace: str
    author: str
    acting_as: str | None
    state: str
    created_at: str
    decided_at: str | None
    policy_reason: str | None


class ProposeOut(BaseModel):
    """Response body for POST /proposals."""

    proposal: ProposalOut
    decision: str


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

    # ------------------------------------------------------------------
    # GET /provenance/{assertion_id}
    # ------------------------------------------------------------------

    @app.get("/provenance/{assertion_id}")
    def provenance_route(
        assertion_id: str,
        _principal: Principal = Depends(_resolve_principal),
    ) -> ProvenanceOut:
        """Return the full provenance record for a single assertion."""
        match = kb.backend.get_assertion(assertion_id)
        if match is None:
            raise NotFoundError(f"Assertion {assertion_id!r} not found")

        review_events = (
            [
                ReviewEventOut(actor=e.actor, type=e.type, detail=e.detail, at=e.at.isoformat())
                for e in kb.backend.get_proposal_events(match.proposal_id)
            ]
            if match.proposal_id
            else []
        )

        return ProvenanceOut(
            id=match.id,
            subject=match.subject,
            predicate=match.predicate,
            value=match.value,
            value_type=match.value_type,
            status=match.status,
            author=match.author,
            confidence=match.confidence,
            source=match.source,
            rationale=match.rationale,
            model=match.model,
            asserted_at=match.asserted_at.isoformat(),
            valid_from=match.valid_from.isoformat() if match.valid_from else None,
            valid_to=match.valid_to.isoformat() if match.valid_to else None,
            proposal_id=match.proposal_id,
            supersedes=match.supersedes,
            review_events=review_events,
        )

    # ------------------------------------------------------------------
    # POST /proposals
    # ------------------------------------------------------------------

    @app.post("/proposals", status_code=201)
    def create_proposal_route(
        body: ProposeIn,
        principal: Principal = Depends(_resolve_principal),
    ) -> ProposeOut:
        """Create a proposal to assert a fact or relation. Does NOT write directly.

        Conflict-routing temporality is resolved server-side from the
        active schema (SPEC §10.1). The acting principal is resolved from
        the bearer token (ADR-0014), never taken from the request body.
        """
        has_literal = body.value is not None and body.value_type is not None
        has_ref = body.target is not None
        if has_literal == has_ref:
            raise ValidationError("Provide exactly one of (value and value_type) or target")

        if has_ref:
            assert body.target is not None
            proposal, decision = kb.propose_ref(
                subject=body.subject,
                predicate=body.predicate,
                target=body.target,
                author=principal.id,
                confidence=body.confidence,
                source=body.source,
                rationale=body.rationale,
                acting_as=body.acting_as,
                model=body.model,
            )
        else:
            assert body.value is not None and body.value_type is not None
            proposal, decision = kb.propose(
                subject=body.subject,
                predicate=body.predicate,
                value=body.value,
                value_type=body.value_type,
                author=principal.id,
                confidence=body.confidence,
                source=body.source,
                rationale=body.rationale,
                acting_as=body.acting_as,
                model=body.model,
            )

        return ProposeOut(
            proposal=ProposalOut(
                id=proposal.id,
                namespace=proposal.namespace,
                author=proposal.author,
                acting_as=proposal.acting_as,
                state=proposal.state,
                created_at=proposal.created_at.isoformat(),
                decided_at=proposal.decided_at.isoformat() if proposal.decided_at else None,
                policy_reason=proposal.policy_reason,
            ),
            decision=type(decision).__name__,
        )

    return app


__all__ = ["create_rest_app"]
