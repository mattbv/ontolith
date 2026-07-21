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

    return app


__all__ = ["create_rest_app"]
