"""Provenance — the derived, one-call audit view for a single assertion.

SPEC §5.4: "Provenance is a derived view ... implementations MUST be able to
return it for any assertion in one call." `Ontology.provenance()` is that
call. It is assembled here once; REST (`GET /provenance/{id}`), GraphQL
(`Query.provenance`), and MCP (`ontolith.provenance`) each shape this into
their own response type rather than re-deriving the assembly (KI-086).

Lives on `Ontology`, not `Entity` — see ADR-0047 for why (`Entity` stays a
frozen value object with no backend handle; SPEC §14.1's `Entity.history()`
sketch is non-normative and predates that decision).
"""

from pydantic import BaseModel, ConfigDict

from ontolith.core.assertion import Assertion
from ontolith.govern.proposal import ProposalEvent


class Provenance(BaseModel):
    """The full provenance record for one assertion (SPEC §5.4).

    Attributes:
        assertion: The assertion itself, carrying every provenance field
            already on it — author, source, confidence, model, rationale,
            the bitemporal window, and its `proposal_id`/`supersedes` links.
        review_events: Every review action recorded against the assertion's
            originating proposal, in occurrence order. Empty when the
            assertion was a direct write (`assert_literal`/`assert_ref`)
            with no proposal behind it.
        superseded_ids: The full predecessor set this assertion superseded
            (KI-008 — `assertion.supersedes` alone records only the first
            predecessor when one incoming assertion supersedes several
            concurrently-overlapping ones). Empty when it superseded nothing.
    """

    model_config = ConfigDict(frozen=True)

    assertion: Assertion
    review_events: tuple[ProposalEvent, ...]
    superseded_ids: tuple[str, ...]


__all__ = ["Provenance"]
