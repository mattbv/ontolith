"""MCP server for Ontolith (ADR-0008, ADR-0014).

Exposes read/propose/flag tools to AI agents. No direct write tool is exposed;
all mutations flow through the proposal/policy pipeline.

Tools (ADR-0008, plus resubmit added for KI-027, retract added for KI-057/ADR-0039):
  ontolith.schema           — read concept/relation definitions
  ontolith.get              — fetch entity + current assertions
  ontolith.query            — symbolic entity retrieval
  ontolith.provenance       — full provenance trail for an assertion
  ontolith.propose          — create a proposal (NOT write)
  ontolith.flag_contradiction — open/extend a contradiction for review
  ontolith.resubmit         — resubmit a changes_requested proposal (NOT write)
  ontolith.retract          — propose retraction of an assertion (NOT write)

Authentication (ADR-0014): every tool, reads included, takes a bearer
``token``. The server resolves the token to a Principal via the injected
AuthProvider — the acting principal is always server-derived from a verified
credential, never client-asserted. One server (one AuthProvider/backend) can
serve many principals, each with their own issued token
(``kb.issue_token(principal_id, author=admin_id)`` via SDK/CLI — requires the
issuing author to hold `admin` capability). Read tools require no further
capability check beyond a resolved principal: capability is a total order
(SPEC §8.3, ``read < propose < write < review < admin``), so any successfully
authenticated principal already clears the "read" floor.

Usage:
    from ontolith.identity.token_auth import TokenAuthProvider
    from ontolith.interfaces.mcp import create_mcp_server
    mcp = create_mcp_server(kb, TokenAuthProvider(kb.backend))
    mcp.run()          # stdio (default for MCP)
    mcp.run("sse")     # SSE transport
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from ontolith.identity.ports import AuthProvider
    from ontolith.ontology import Ontology


def create_mcp_server(kb: Ontology, auth_provider: AuthProvider, name: str = "ontolith") -> FastMCP:
    """Build and return a FastMCP server bound to the given knowledge base.

    The returned server is not yet running — call ``mcp.run()`` to start it.

    Args:
        kb: Ontology instance (knowledge base) to expose
        auth_provider: Resolves caller-supplied bearer tokens to Principals
            (ADR-0014) — e.g. ``TokenAuthProvider(kb.backend)``
        name: Server name advertised to MCP clients

    Returns:
        Configured FastMCP server with all ADR-0008 tools registered
    """
    mcp: FastMCP = FastMCP(name)

    # ------------------------------------------------------------------
    # ontolith.schema — list concepts and their properties/relations
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.schema")
    def schema_tool(token: str, namespace: str = "default") -> dict[str, Any]:
        """Return the schema (concepts, properties, and relations) for a namespace.

        Args:
            token: Bearer token identifying the calling principal (ADR-0014)
            namespace: Namespace to inspect (default: "default")

        Returns:
            Dict with "concepts" key listing concept names, their property
            definitions, and their relation definitions from the active
            schema version (SPEC §14.4), or "error" if the token does not
            resolve to a valid principal.
        """
        from ontolith.core.errors import AuthError

        try:
            auth_provider.resolve(token)
        except AuthError as exc:
            return {"error": str(exc), "code": "auth_error"}

        ir = kb.backend.get_schema(namespace)
        if ir is None:
            return {"concepts": []}
        concepts = []
        for concept_name, concept_def in ir.concepts.items():
            concepts.append(
                {
                    "name": concept_name,
                    "properties": [
                        {
                            "name": prop_name,
                            "type": prop_def.value_type,
                            "cardinality": prop_def.cardinality,
                            "temporality": prop_def.temporality,
                            "required": prop_def.required,
                        }
                        for prop_name, prop_def in concept_def.properties.items()
                    ],
                    "relations": [
                        {
                            "name": rel_name,
                            "target_concept": rel_def.target_concept,
                            "cardinality": rel_def.cardinality,
                            "required": rel_def.required,
                            "temporality": rel_def.temporality,
                            "inverse": rel_def.inverse,
                        }
                        for rel_name, rel_def in concept_def.relations.items()
                    ],
                }
            )
        return {"namespace": namespace, "version": ir.version, "concepts": concepts}

    # ------------------------------------------------------------------
    # ontolith.get — fetch entity + active assertions
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.get")
    def get_tool(entity_id: str, token: str) -> dict[str, Any]:
        """Fetch an entity and its currently active assertions.

        Args:
            entity_id: Entity ID to retrieve
            token: Bearer token identifying the calling principal (ADR-0014)

        Returns:
            Dict with "entity" and "assertions" keys, or "error" if not found
            or the token does not resolve to a valid principal.
        """
        from ontolith.core.errors import AuthError

        try:
            auth_provider.resolve(token)
        except AuthError as exc:
            return {"error": str(exc), "code": "auth_error"}

        entity = kb.backend.get_entity(entity_id)
        if entity is None:
            return {"error": f"Entity {entity_id!r} not found"}

        assertions = kb.backend.assertions(subject=entity_id, status="active")
        return {
            "entity": {
                "id": entity.id,
                "concept": entity.concept,
                "namespace": entity.namespace,
                "natural_key": entity.natural_key,
                "created_at": entity.created_at.isoformat(),
                "created_by": entity.created_by,
            },
            "assertions": [
                {
                    "id": a.id,
                    "predicate": a.predicate,
                    "value": a.value,
                    "value_type": a.value_type,
                    "confidence": a.confidence,
                    "author": a.author,
                    "asserted_at": a.asserted_at.isoformat(),
                }
                for a in assertions
            ],
        }

    # ------------------------------------------------------------------
    # ontolith.query — symbolic entity retrieval
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.query")
    def query_tool(
        concept: str,
        token: str,
        filters: dict[str, str] | None = None,
        namespace: str = "default",
    ) -> dict[str, Any]:
        """Query entities of a concept, optionally filtered by property values.

        Args:
            concept: Concept name to query (e.g. "Person")
            token: Bearer token identifying the calling principal (ADR-0014)
            filters: Optional dict of property/relation name → value
                (equality) — a relation filter matches the relation's
                target entity id (e.g. {"employer": "org-123"}) — or
                property__op → value using a closed set of lookup-operator
                suffixes (KI-039): __contains (substring), __gt/__lt/__gte/
                __lte (numeric range, schema-declared Integer/Float
                properties only), e.g. {"age__gte": 18}. Multi-hop
                double-underscore keys (e.g. "employer__name") and any
                suffix outside this closed operator set are not supported
                and raise a validation_error (ADR-0027, KI-030).
            namespace: Namespace to query (default: "default")

        Returns:
            Dict with "entities" list and "count", or "error" if the token
            does not resolve to a valid principal or a filter key is invalid.
        """
        from ontolith.core.errors import AuthError, ValidationError

        try:
            auth_provider.resolve(token)
        except AuthError as exc:
            return {"error": str(exc), "code": "auth_error"}

        builder = kb.query(concept)
        try:
            if filters:
                builder = builder.where(**filters)
            entities = builder.all()
        except ValidationError as exc:
            return {"error": str(exc), "code": "validation_error"}
        return {
            "concept": concept,
            "count": len(entities),
            "entities": [
                {
                    "id": e.id,
                    "concept": e.concept,
                    "natural_key": e.natural_key,
                    "created_at": e.created_at.isoformat(),
                }
                for e in entities
            ],
        }

    # ------------------------------------------------------------------
    # ontolith.provenance — full trail for an assertion
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.provenance")
    def provenance_tool(assertion_id: str, token: str) -> dict[str, Any]:
        """Return the full provenance record for a single assertion.

        Args:
            assertion_id: Assertion ID to inspect
            token: Bearer token identifying the calling principal (ADR-0014)

        Returns:
            Dict with assertion details including author, confidence, source,
            rationale, proposal link, temporal fields, and superseded_ids
            (the full predecessor set this assertion superseded — supersedes
            alone only records the first, see KI-008), or "error" if not
            found or the token does not resolve to a valid principal.
        """
        from ontolith.core.errors import AuthError

        try:
            auth_provider.resolve(token)
        except AuthError as exc:
            return {"error": str(exc), "code": "auth_error"}

        match = kb.backend.get_assertion(assertion_id)
        if match is None:
            return {"error": f"Assertion {assertion_id!r} not found"}

        review_events = (
            [
                {
                    "actor": e.actor,
                    "type": e.type,
                    "detail": e.detail,
                    "at": e.at.isoformat(),
                }
                for e in kb.backend.get_proposal_events(match.proposal_id)
            ]
            if match.proposal_id
            else []
        )

        superseded_ids = [
            e.assertion_id for e in kb.backend.get_assertion_events_by_successor(match.id)
        ]

        return {
            "id": match.id,
            "subject": match.subject,
            "predicate": match.predicate,
            "value": match.value,
            "value_type": match.value_type,
            "status": match.status,
            "author": match.author,
            "confidence": match.confidence,
            "source": match.source,
            "rationale": match.rationale,
            "model": match.model,
            "asserted_at": match.asserted_at.isoformat(),
            "valid_from": match.valid_from.isoformat() if match.valid_from else None,
            "valid_to": match.valid_to.isoformat() if match.valid_to else None,
            "proposal_id": match.proposal_id,
            "supersedes": match.supersedes,
            "superseded_ids": superseded_ids,
            "review_events": review_events,
        }

    # ------------------------------------------------------------------
    # ontolith.propose — create a proposal (NOT a direct write)
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.propose")
    def propose_tool(
        subject: str,
        predicate: str,
        token: str,
        value: str | None = None,
        value_type: str | None = None,
        target: str | None = None,
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
        acting_as: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Create a proposal to assert a fact or relation. Does NOT write directly.

        The proposal is evaluated by the policy engine:
        - Trusted principals → auto_accepted (assertion written immediately)
        - AI/low-trust principals → require_review (queued for human review)
        - Read-only principals → rejected

        Conflict-routing temporality is resolved server-side from the active
        schema's declaration for ``predicate`` (SPEC §10.1) — it is not a
        caller-supplied argument.

        The acting principal is resolved from ``token`` (ADR-0014), never
        taken as a caller-supplied ID. When ``acting_as`` is set (delegation),
        policy is evaluated using min(capability(author), capability(acting_as))
        (SPEC §8.4, ADR-0003). ``model`` (the AI model family+version) is
        REQUIRED when the resolved principal is ``ai``-kind (SPEC §7.4/§14.4).

        Args:
            subject: Entity ID to assert about
            predicate: Predicate name (e.g. "Person.name" or "Person.employer")
            token: Bearer token identifying the calling principal (ADR-0014)
            value: Literal value to assert (mutually exclusive with target)
            value_type: Type of value (e.g. "Text", "Integer", "Date"); required with value
            target: Target entity ID for a relation (mutually exclusive with value)
            confidence: Optional confidence score (0.0–1.0)
            source: Optional source URL or reference
            rationale: Optional explanation for the assertion
            acting_as: Optional principal ID being acted on behalf of (delegation)
            model: Model family+version; required when the calling principal is AI-kind

        Returns:
            Dict with "proposal" (id, state, policy_reason, acting_as) and "decision" type.
        """
        from ontolith.core.errors import AuthError, CapabilityError, ValidationError

        has_literal = value is not None and value_type is not None
        has_ref = target is not None
        if has_literal == has_ref:
            return {
                "error": "Provide exactly one of (value and value_type) or target",
                "code": "validation_error",
            }

        try:
            author = auth_provider.resolve(token).id
        except AuthError as exc:
            return {"error": str(exc), "code": "auth_error"}

        try:
            if has_ref:
                assert target is not None
                proposal, decision = kb.propose_ref(
                    subject=subject,
                    predicate=predicate,
                    target=target,
                    author=author,
                    confidence=confidence,
                    source=source,
                    rationale=rationale,
                    acting_as=acting_as,
                    model=model,
                )
            else:
                assert value is not None and value_type is not None
                proposal, decision = kb.propose(
                    subject=subject,
                    predicate=predicate,
                    value=value,
                    value_type=value_type,
                    author=author,
                    confidence=confidence,
                    source=source,
                    rationale=rationale,
                    acting_as=acting_as,
                    model=model,
                )
        except AuthError as exc:
            return {"error": str(exc), "code": "auth_error"}
        except CapabilityError as exc:
            return {"error": str(exc), "code": "capability_error"}
        except ValidationError as exc:
            return {"error": str(exc), "code": "validation_error"}

        return {
            "proposal": {
                "id": proposal.id,
                "state": proposal.state,
                "policy_reason": proposal.policy_reason,
                "decided_at": proposal.decided_at.isoformat() if proposal.decided_at else None,
                "acting_as": proposal.acting_as,
            },
            "decision": type(decision).__name__,
        }

    # ------------------------------------------------------------------
    # ontolith.retract — propose retraction of an assertion (NOT a direct write)
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.retract")
    def retract_tool(
        assertion_id: str,
        token: str,
        acting_as: str | None = None,
    ) -> dict[str, Any]:
        """Propose retraction of an assertion through the governed
        proposal/policy pipeline (SPEC §9). Does NOT delete or write
        directly — same propose/policy/conflict-routing pipeline as
        ``ontolith.propose``, exposed at the same propose capability tier
        (ADR-0008/ADR-0039), unlike ``resolve_contradiction`` which stays
        reviewer-only and is deliberately not exposed via MCP at all.

        The acting principal is resolved from ``token`` (ADR-0014), never
        taken as a caller-supplied ID. When ``acting_as`` is set
        (delegation), the retraction is made on behalf of another
        principal (SPEC §8.4, ADR-0003).

        Args:
            assertion_id: ID of the assertion to retract
            token: Bearer token identifying the calling principal (ADR-0014)
            acting_as: Optional principal ID being acted on behalf of (delegation)

        Returns:
            Dict with "proposal" (id, state, policy_reason, decided_at) and
            "decision" type, or "error".

        Note:
            Unlike ``ontolith.resubmit``, ``retract()`` cannot raise
            ``NotFoundError``/``ValidationError`` — an unknown
            ``assertion_id`` surfaces as ``StorageError`` from the backend
            write itself (pre-existing, not specific to this tool; REST's
            generic ``OntolithError`` handler maps it to a redacted 500
            the same way). Only ``AuthError`` (bad token, or an unknown
            ``acting_as``) and ``CapabilityError`` (delegation not owned,
            or the KI-033/KI-043 contradiction party/floor guards) are
            actually reachable from ``kb.retract()``, so those are the
            only two caught here.
        """
        from ontolith.core.errors import AuthError, CapabilityError

        try:
            author = auth_provider.resolve(token).id
        except AuthError as exc:
            return {"error": str(exc), "code": "auth_error"}

        try:
            proposal, decision = kb.retract(assertion_id, author=author, acting_as=acting_as)
        except AuthError as exc:
            return {"error": str(exc), "code": "auth_error"}
        except CapabilityError as exc:
            return {"error": str(exc), "code": "capability_error"}

        return {
            "proposal": {
                "id": proposal.id,
                "state": proposal.state,
                "policy_reason": proposal.policy_reason,
                "decided_at": proposal.decided_at.isoformat() if proposal.decided_at else None,
                "acting_as": proposal.acting_as,
            },
            "decision": type(decision).__name__,
        }

    # ------------------------------------------------------------------
    # ontolith.flag_contradiction — open/extend a contradiction
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.flag_contradiction")
    def flag_contradiction_tool(
        assertion_id_a: str,
        assertion_id_b: str,
        token: str,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        """Flag two assertions as contradictory and route them to review.

        Opens a new contradiction (or extends an existing open one) for the
        subject+predicate pair. Both assertions are marked "flagged" and
        excluded from default queries until the contradiction is resolved.

        This is a propose-level action (requires >= propose capability) — it
        does NOT resolve the contradiction. The acting principal is resolved
        from ``token`` (ADR-0014), never taken as a caller-supplied ID.

        Args:
            assertion_id_a: First conflicting assertion ID
            assertion_id_b: Second conflicting assertion ID
            token: Bearer token identifying the calling principal (ADR-0014)
            rationale: Optional explanation of the contradiction

        Returns:
            Dict describing the contradiction created/extended, or "error".
        """
        from ontolith.core.errors import AuthError, CapabilityError, NotFoundError, ValidationError

        try:
            author = auth_provider.resolve(token).id
        except AuthError as exc:
            return {"error": str(exc), "code": "auth_error"}

        try:
            contradiction, action = kb.flag_contradiction(
                assertion_id_a, assertion_id_b, author, rationale=rationale
            )
        except AuthError as exc:
            return {"error": str(exc), "code": "auth_error"}
        except CapabilityError as exc:
            return {"error": str(exc), "code": "capability_error"}
        except NotFoundError as exc:
            return {"error": str(exc), "code": "not_found"}
        except ValidationError as exc:
            return {"error": str(exc), "code": "validation_error"}

        return {
            "contradiction_id": contradiction.id,
            "subject": contradiction.subject,
            "predicate": contradiction.predicate,
            "member_ids": [assertion_id_a, assertion_id_b],
            "action": action,
            "raised_by": contradiction.raised_by,
        }

    # ------------------------------------------------------------------
    # ontolith.resubmit — resubmit a changes_requested proposal (NOT write)
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.resubmit")
    def resubmit_tool(proposal_id: str, token: str) -> dict[str, Any]:
        """Resubmit a proposal after changes were requested (KI-027).

        Re-evaluates policy against a fresh view of the knowledge base,
        same as ``ontolith.propose`` — this is not a direct write. Only the
        proposal's own author (or delegating principal) may call this; the
        acting principal is resolved from ``token`` (ADR-0014), never taken
        as a caller-supplied ID. This is the only MCP-exposed way for an AI
        principal to act on a ``changes_requested`` proposal it authored —
        AI proposals always route to require_review (ADR-0003), so an AI
        author reaching changes_requested has no write capability to fall
        back on outside this tool.

        Args:
            proposal_id: ID of the proposal to resubmit
            token: Bearer token identifying the calling principal (ADR-0014)

        Returns:
            Dict with "proposal" (id, state, policy_reason, decided_at) and
            "decision" type, or "error".
        """
        from ontolith.core.errors import AuthError, CapabilityError, NotFoundError, ValidationError

        try:
            author = auth_provider.resolve(token).id
        except AuthError as exc:
            return {"error": str(exc), "code": "auth_error"}

        try:
            proposal, decision = kb.resubmit(proposal_id, author)
        except AuthError as exc:
            return {"error": str(exc), "code": "auth_error"}
        except CapabilityError as exc:
            return {"error": str(exc), "code": "capability_error"}
        except NotFoundError as exc:
            return {"error": str(exc), "code": "not_found"}
        except ValidationError as exc:
            return {"error": str(exc), "code": "validation_error"}

        return {
            "proposal": {
                "id": proposal.id,
                "state": proposal.state,
                "policy_reason": proposal.policy_reason,
                "decided_at": proposal.decided_at.isoformat() if proposal.decided_at else None,
            },
            "decision": type(decision).__name__,
        }

    return mcp


__all__ = ["create_mcp_server"]
