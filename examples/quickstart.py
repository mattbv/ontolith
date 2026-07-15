#!/usr/bin/env python3
"""Quickstart example - basic Ontolith usage.

This example demonstrates:
- Creating a knowledge base
- Defining a schema with the class DSL
- Creating principals (human and AI)
- Creating entities
- Making assertions (literal and reference)
- Querying assertions
- Provenance tracking
"""

import tempfile
from pathlib import Path

from ontolith import Ontology
from ontolith.schema import Concept, Date, Ref, Relation, Text, compile_schema


def main() -> None:
    """Run the quickstart example."""
    # Create a temporary database for this example
    db_path = Path(tempfile.mktemp(suffix=".db"))

    print("🚀 Ontolith Quickstart Example\n")
    print(f"Database: {db_path}\n")

    # Connect to knowledge base
    kb = Ontology.connect(db_path)
    print("✓ Connected to knowledge base\n")

    # Create principals
    print("Creating principals...")
    alice = kb.create_principal(
        "alice@example.com",
        kind="human",
        auth_method="oidc",
        default_capability="admin",
        trust_level=8,
    )
    print(f"✓ Created human principal: {alice.id}")

    research_bot = kb.create_principal(
        "research-bot",
        kind="ai",
        owner="alice@example.com",
        auth_method="workload",
        default_capability="propose",
        trust_level=5,
    )
    print(f"✓ Created AI principal: {research_bot.id} (owner: {research_bot.owner})")
    print()

    # Define and apply a schema via the class DSL (SPEC §6.2)
    print("Defining schema...")

    class InventedThing(Concept):
        name: Text

    class Person(Concept):
        name: Text
        born: Date | None = None
        collaboratedWith: Ref["Person"] = Relation()
        contributedTo: Ref["InventedThing"] = Relation()

    schema = compile_schema("default", 1, Person, InventedThing)
    kb.apply_schema(schema, author=alice.id)
    print(f"✓ Applied schema version {schema.version}: {', '.join(schema.concepts)}")
    print()

    # Create entities
    print("Creating entities...")
    ada = kb.create_entity(
        concept="Person",
        author=alice.id,
        natural_key="ada-lovelace",
    )
    print(f"✓ Created Person entity: {ada.id}")

    charles = kb.create_entity(
        concept="Person",
        author=alice.id,
        natural_key="charles-babbage",
    )
    print(f"✓ Created Person entity: {charles.id}")

    analytical_engine = kb.create_entity(
        concept="InventedThing",
        author=alice.id,
        natural_key="analytical-engine",
    )
    print(f"✓ Created InventedThing entity: {analytical_engine.id}")
    print()

    # Make literal assertions
    print("Making literal assertions...")
    kb.assert_literal(
        subject=ada.id,
        predicate="Person.name",
        value="Ada Lovelace",
        value_type="Text",
        author=alice.id,
        source="Wikipedia",
        confidence=1.0,
    )
    print(f"✓ Asserted: {ada.id} name = 'Ada Lovelace'")

    kb.assert_literal(
        subject=ada.id,
        predicate="Person.born",
        value="1815-12-10",
        value_type="Date",
        author=alice.id,
        source="Wikipedia",
        confidence=1.0,
    )
    print(f"✓ Asserted: {ada.id} born = '1815-12-10'")

    kb.assert_literal(
        subject=charles.id,
        predicate="Person.name",
        value="Charles Babbage",
        value_type="Text",
        author=alice.id,
        source="Wikipedia",
        confidence=1.0,
    )
    print(f"✓ Asserted: {charles.id} name = 'Charles Babbage'")

    kb.assert_literal(
        subject=analytical_engine.id,
        predicate="InventedThing.name",
        value="Analytical Engine",
        value_type="Text",
        author=alice.id,
        source="Wikipedia",
        confidence=1.0,
    )
    print(f"✓ Asserted: {analytical_engine.id} name = 'Analytical Engine'")
    print()

    # Make reference assertions (relations)
    print("Making reference assertions (relations)...")
    kb.assert_ref(
        subject=ada.id,
        predicate="Person.collaboratedWith",
        target=charles.id,
        author=alice.id,
        source="Historical records",
        confidence=1.0,
    )
    print(f"✓ Asserted: {ada.id} collaboratedWith {charles.id}")

    # AI principals cannot write directly (SPEC §9.3) — they go through the
    # governed propose path instead, and always require human review
    # regardless of trust level (ADR-0003). `model` is required provenance
    # for AI-authored assertions (SPEC §7.4).
    proposal, decision = kb.propose_ref(
        subject=ada.id,
        predicate="Person.contributedTo",
        target=analytical_engine.id,
        author=research_bot.id,
        source="ACM Digital Library",
        confidence=0.95,
        model="claude-sonnet-4-20250101",
    )
    print(f"✓ Proposed: {ada.id} contributedTo {analytical_engine.id}")
    print(f"  (by AI: {research_bot.id}, confidence: 0.95)")
    print(f"  Decision: {type(decision).__name__} — proposal state: {proposal.state}")
    print()

    # Query assertions
    print("Querying assertions...")
    ada_facts = kb.assertions(subject=ada.id)
    print(f"✓ Found {len(ada_facts)} facts about Ada Lovelace:")
    for fact in ada_facts:
        if fact.value_kind == "literal":
            print(f"  - {fact.predicate}: '{fact.value}' (by {fact.author})")
        else:
            print(f"  - {fact.predicate}: → {fact.value} (by {fact.author})")
    print()

    # Provenance tracking
    print("Provenance tracking...")
    name_assertion = next(a for a in ada_facts if a.predicate == "Person.name")
    print(f"✓ Assertion '{name_assertion.id}' provenance:")
    print(f"  Author: {name_assertion.author}")
    print(f"  Source: {name_assertion.source}")
    print(f"  Confidence: {name_assertion.confidence}")
    print(f"  Asserted at: {name_assertion.asserted_at}")
    print(f"  Valid from: {name_assertion.valid_from}")
    print(f"  Status: {name_assertion.status}")
    print()

    # Retrieve principals
    print("Retrieving principals...")
    retrieved_alice = kb.get_principal(alice.id)
    assert retrieved_alice is not None
    print(f"✓ Retrieved principal: {retrieved_alice.id}")
    print(f"  Kind: {retrieved_alice.kind}")
    print(f"  Capability: {retrieved_alice.default_capability}")
    print(f"  Trust level: {retrieved_alice.trust_level}")
    print()

    # Clean up
    kb.close()
    db_path.unlink()
    print("✅ Quickstart complete! Knowledge base created, queried, and cleaned up.")


if __name__ == "__main__":
    main()
