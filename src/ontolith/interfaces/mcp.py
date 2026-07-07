"""MCP server for Ontolith (ADR-0008).

Exposes read/propose/flag tools to AI agents. No direct write tool is exposed;
all mutations flow through the proposal/policy pipeline.

Tools (ADR-0008):
  ontolith.schema           — read concept/relation definitions
  ontolith.get              — fetch entity + current assertions
  ontolith.query            — symbolic entity retrieval
  ontolith.provenance       — full provenance trail for an assertion
  ontolith.propose          — create a proposal (NOT write)
  ontolith.flag_contradiction — open/extend a contradiction for review

Usage:
    from ontolith.interfaces.mcp import create_mcp_server
    mcp = create_mcp_server(kb)
    mcp.run()          # stdio (default for MCP)
    mcp.run("sse")     # SSE transport
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from ontolith.ontology import Ontology


def create_mcp_server(kb: Ontology, name: str = "ontolith") -> FastMCP:
    """Build and return a FastMCP server bound to the given knowledge base.

    The returned server is not yet running — call ``mcp.run()`` to start it.

    Args:
        kb: Ontology instance (knowledge base) to expose
        name: Server name advertised to MCP clients

    Returns:
        Configured FastMCP server with all ADR-0008 tools registered
    """
    mcp: FastMCP = FastMCP(name)

    # ------------------------------------------------------------------
    # ontolith.schema — list concepts and their properties
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.schema")
    def schema_tool(namespace: str = "default") -> dict[str, Any]:
        """Return the schema (concepts and their properties) for a namespace.

        Args:
            namespace: Namespace to inspect (default: "default")

        Returns:
            Dict with "concepts" key listing concept names and their
            property definitions from the active schema version.
        """
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
                            "temporality": prop_def.temporality,
                            "required": prop_def.required,
                        }
                        for prop_name, prop_def in concept_def.properties.items()
                    ],
                }
            )
        return {"namespace": namespace, "version": ir.version, "concepts": concepts}

    # ------------------------------------------------------------------
    # ontolith.get — fetch entity + active assertions
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.get")
    def get_tool(entity_id: str) -> dict[str, Any]:
        """Fetch an entity and its currently active assertions.

        Args:
            entity_id: Entity ID to retrieve

        Returns:
            Dict with "entity" and "assertions" keys, or "error" if not found.
        """
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
        filters: dict[str, str] | None = None,
        namespace: str = "default",
    ) -> dict[str, Any]:
        """Query entities of a concept, optionally filtered by property values.

        Args:
            concept: Concept name to query (e.g. "Person")
            filters: Optional dict of property_name → value (e.g. {"name": "Ada"})
            namespace: Namespace to query (default: "default")

        Returns:
            Dict with "entities" list and "count".
        """
        builder = kb.query(concept)
        if filters:
            builder = builder.where(**filters)
        entities = builder.all()
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
    def provenance_tool(assertion_id: str) -> dict[str, Any]:
        """Return the full provenance record for a single assertion.

        Args:
            assertion_id: Assertion ID to inspect

        Returns:
            Dict with assertion details including author, confidence, source,
            rationale, proposal link, and temporal fields.
        """
        results = kb.backend.assertions(status=None)
        match = next((a for a in results if a.id == assertion_id), None)
        if match is None:
            return {"error": f"Assertion {assertion_id!r} not found"}

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
            "asserted_at": match.asserted_at.isoformat(),
            "valid_from": match.valid_from.isoformat() if match.valid_from else None,
            "valid_to": match.valid_to.isoformat() if match.valid_to else None,
            "proposal_id": match.proposal_id,
            "supersedes": match.supersedes,
        }

    # ------------------------------------------------------------------
    # ontolith.propose — create a proposal (NOT a direct write)
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.propose")
    def propose_tool(
        subject: str,
        predicate: str,
        value: str,
        value_type: str,
        author: str,
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
        acting_as: str | None = None,
    ) -> dict[str, Any]:
        """Create a proposal to assert a fact. Does NOT write directly.

        The proposal is evaluated by the policy engine:
        - Trusted principals → auto_accepted (assertion written immediately)
        - AI/low-trust principals → require_review (queued for human review)
        - Read-only principals → rejected

        Conflict-routing temporality is resolved server-side from the active
        schema's declaration for ``predicate`` (SPEC §10.1) — it is not a
        caller-supplied argument.

        When ``acting_as`` is set (delegation), policy is evaluated using the
        delegating principal's capability and trust level (ADR-0003).

        Args:
            subject: Entity ID to assert about
            predicate: Predicate name (e.g. "Person.name")
            value: Literal value to assert
            value_type: Type of value (e.g. "Text", "Integer", "Date")
            author: Principal ID making the assertion
            confidence: Optional confidence score (0.0–1.0)
            source: Optional source URL or reference
            rationale: Optional explanation for the assertion
            acting_as: Optional principal ID being acted on behalf of (delegation)

        Returns:
            Dict with "proposal" (id, state, policy_reason, acting_as) and "decision" type.
        """
        from ontolith.core.errors import AuthError, CapabilityError

        try:
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
            )
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
        author: str,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        """Flag two assertions as contradictory and route them to review.

        Opens a new contradiction (or extends an existing open one) for the
        subject+predicate pair. Both assertions are marked "flagged" and
        excluded from default queries until the contradiction is resolved.

        This is a propose-level action — it does NOT resolve the contradiction.

        Args:
            assertion_id_a: First conflicting assertion ID
            assertion_id_b: Second conflicting assertion ID
            author: Principal ID raising the flag
            rationale: Optional explanation of the contradiction

        Returns:
            Dict describing the contradiction created/extended, or "error".
        """

        principal = kb.backend.get_principal(author)
        if principal is None:
            return {"error": f"Principal {author!r} not found", "code": "auth_error"}

        # Resolve both assertions (status=None: a flagged/superseded assertion
        # must still be resolvable here, e.g. when extending an open contradiction)
        all_assertions = kb.backend.assertions(status=None)
        a_map = {a.id: a for a in all_assertions}

        a = a_map.get(assertion_id_a)
        b = a_map.get(assertion_id_b)

        if a is None:
            return {"error": f"Assertion {assertion_id_a!r} not found", "code": "not_found"}
        if b is None:
            return {"error": f"Assertion {assertion_id_b!r} not found", "code": "not_found"}
        if a.subject != b.subject or a.predicate != b.predicate:
            return {
                "error": "Assertions must share the same subject and predicate to contradict",
                "code": "validation_error",
            }

        # Find or create contradiction
        from ontolith.govern.contradiction import Contradiction

        existing = kb.backend.get_open_contradiction(
            namespace=kb.namespace,
            subject=a.subject,
            predicate=a.predicate,
        )

        with kb.backend.transaction():
            if existing is not None:
                new_member_ids = list(
                    dict.fromkeys(existing.member_ids + [assertion_id_a, assertion_id_b])
                )
                kb.backend.update_contradiction_members(existing.id, new_member_ids)
                contradiction_id = existing.id
            else:
                contradiction_id = kb.id_provider.next()
                contradiction = Contradiction(
                    id=contradiction_id,
                    namespace=kb.namespace,
                    subject=a.subject,
                    predicate=a.predicate,
                    member_ids=[assertion_id_a, assertion_id_b],
                    state="open",
                    created_at=kb.clock.now(),
                )
                kb.backend.put_contradiction(contradiction)

            for aid in [assertion_id_a, assertion_id_b]:
                if a_map[aid].status != "flagged":
                    kb.backend.set_assertion_status(aid, "flagged")

        return {
            "contradiction_id": contradiction_id,
            "subject": a.subject,
            "predicate": a.predicate,
            "member_ids": [assertion_id_a, assertion_id_b],
            "action": "extended" if existing else "created",
        }

    return mcp


__all__ = ["create_mcp_server"]
