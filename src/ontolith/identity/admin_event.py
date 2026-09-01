"""AdminEvent - append-only audit record of an admin-tier action (KI-060, SPEC §17).

`assertion_event`/`proposal_event` (core/assertion.py, govern/proposal.py) make
writes and review decisions attributable; this closes the equivalent gap for
the highest-stakes actions in the system, which previously left no trace at
all: principal creation, schema application, and plugin registration.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

AdminAction = Literal["create_principal", "apply_schema", "register_plugin"]


class AdminEvent(BaseModel):
    """A single admin-tier action, recorded for forensic traceability.

    Deliberately does NOT cover token issuance/revocation — those are
    attributed directly on `PrincipalCredential.issued_by`/`.revoked_by`
    instead, since every credential row already has a natural home for its
    own attribution and doesn't need a separate event row to point back to.

    `target` is free text, not a foreign key: unlike `assertion_event`/
    `proposal_event` (each referencing exactly one row type), the three
    actions here target a principal id, a `namespace:version` schema
    descriptor, and a plugin name respectively — no single column type
    could reference all three, so this follows `ProposalEvent.type`'s own
    precedent of trusting Pydantic's `Literal` at construction rather than
    a DB-level CHECK/FOREIGN KEY.

    Attributes:
        id: Unique event ID (ULID)
        actor: Principal ID who performed the action (admin capability
            required for apply_schema/register_plugin, checked by the
            caller before this event is ever constructed; create_principal
            itself is deliberately ungated per ADR-0022, so `actor` there
            is whatever the caller optionally supplied, unverified)
        action: Which admin action this event records
        target: Free-text identifier of what was acted on — a principal
            id, a `namespace:vN` schema descriptor, or a plugin name
        at: When the action occurred
        detail: Optional free-text detail
    """

    id: str
    actor: str
    action: AdminAction
    target: str
    at: datetime
    detail: str | None = None

    model_config = ConfigDict(frozen=True)


__all__ = ["AdminAction", "AdminEvent"]
