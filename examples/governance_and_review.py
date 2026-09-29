#!/usr/bin/env python3
"""Governance & review workflow example.

This example demonstrates:
- Why AI principals can't write directly (SPEC §9.3) and always go
  through the governed propose() path
- ThresholdPolicy's default behavior: human writes auto-accept, AI
  proposals always require review (ADR-0003)
- The full review lifecycle: propose -> request_changes -> resubmit
  -> accept
- Rejecting a proposal outright
"""

import tempfile
from pathlib import Path

from ontolith import Ontology
from ontolith.govern import AutoAccept, RequireReview
from ontolith.schema import Concept, Text, compile_schema


def main() -> None:
    """Run the governance & review example."""
    db_path = Path(tempfile.mktemp(suffix=".db"))
    print("🏛️  Ontolith Governance & Review Example\n")

    kb = Ontology.connect(db_path)

    # A human reviewer with write capability, and an AI principal that
    # only ever proposes (SPEC §8.1's capability lattice: read < propose
    # < write < review < admin).
    reviewer = kb.create_principal(
        "reviewer@example.com", kind="human", default_capability="admin", trust_level=8
    )
    researcher_bot = kb.create_principal(
        "researcher-bot",
        kind="ai",
        owner=reviewer.id,
        default_capability="propose",
        trust_level=6,
    )

    class Company(Concept):
        name: Text
        headquarters: Text | None = None

    schema = compile_schema("default", 1, Company)
    kb.apply_schema(schema, author=reviewer.id)

    acme = kb.create_entity(concept="Company", author=reviewer.id, natural_key="acme-corp")
    kb.assert_literal(
        subject=acme.id,
        predicate="Company.name",
        value="Acme Corp",
        value_type="Text",
        author=reviewer.id,
        confidence=1.0,
    )

    # --- A human write auto-accepts under the default ThresholdPolicy ---
    proposal, decision = kb.propose(
        subject=acme.id,
        predicate="Company.headquarters",
        value="Springfield",
        value_type="Text",
        author=reviewer.id,
        source="internal records",
        confidence=1.0,
    )
    print(f"Human propose(): decision = {type(decision).__name__} ({proposal.state})")
    assert isinstance(decision, AutoAccept)

    # --- An AI proposal ALWAYS requires review, regardless of trust level ---
    # `model` is required provenance for AI-authored assertions (SPEC §7.4).
    proposal, decision = kb.propose(
        subject=acme.id,
        predicate="Company.headquarters",
        value="Metropolis",  # deliberately conflicting, for the story below
        value_type="Text",
        author=researcher_bot.id,
        source="a news article",
        confidence=0.7,
        model="claude-sonnet-4-20250101",
    )
    print(f"AI propose():    decision = {type(decision).__name__} ({proposal.state})")
    assert isinstance(decision, RequireReview)
    print(f"  Proposal {proposal.id} is now waiting for a human reviewer.\n")

    # --- The reviewer isn't convinced by the source; request changes ---
    proposal = kb.request_changes(
        proposal.id, reviewer=reviewer.id, reason="Need a primary source, not a news article."
    )
    print(f"request_changes(): proposal is now {proposal.state!r}")

    # --- The author (or a delegate) resubmits with a better rationale.
    # resubmit() replays the ORIGINAL payload through a FRESH policy
    # evaluation -- it doesn't let the author quietly change the value. ---
    proposal, decision = kb.resubmit(proposal.id, author=researcher_bot.id)
    print(f"resubmit():         decision = {type(decision).__name__} ({proposal.state})")

    # --- The reviewer accepts it this time ---
    if isinstance(decision, RequireReview):
        proposal = kb.accept_proposal(proposal.id, reviewer=reviewer.id)
        print(f"accept_proposal():  proposal is now {proposal.state!r}")

    print()

    # Both values now coexist as a flagged CONTRADICTION (SPEC §10.3) --
    # Company.headquarters is a static property by default, and static
    # properties are never silently overwritten when a differing value
    # arrives. A reviewer resolves it explicitly with resolve_contradiction().
    # kb.assertions() defaults to status="active" -- flagged contradiction
    # members are deliberately excluded unless asked for explicitly
    # (SPEC §10.3: "MUST be excluded from default retrieval").
    active = kb.assertions(subject=acme.id, predicate="Company.headquarters", status="active")
    flagged = kb.assertions(subject=acme.id, predicate="Company.headquarters", status="flagged")
    print(f"Active headquarters assertions:  {[f.value for f in active]}")
    print(f"Flagged (contradicting) values:  {[f.value for f in flagged]}")

    kb.close()
    db_path.unlink()
    print("\n✅ Governance & review example complete!")


if __name__ == "__main__":
    main()
