#!/usr/bin/env python3
"""Hybrid (symbolic + vector) search example.

This example demonstrates:
- `.semantic()`: vector similarity search over Text-typed content
  (SPEC §11.3), using the default `HashingEmbedder` -- no ML model
  download needed for this example, though a real deployment would
  configure a production Embedder instead
- `reindex()`: the only way vectors enter the index (no write path
  auto-embeds)
- `.semantic()` intersected with a symbolic `.where()` filter
- `.min_confidence()`: filtering by provenance signals, not just content
  (`.trust_at_least()` filters the same way on the asserting principal's
  trust level instead -- see the Hybrid Search tutorial on the docs site)
"""

import tempfile
from pathlib import Path

from ontolith import Ontology
from ontolith.schema import Concept, Integer, Text, compile_schema


def main() -> None:
    """Run the hybrid search example."""
    db_path = Path(tempfile.mktemp(suffix=".db"))
    print("🔍 Ontolith Hybrid Search Example\n")

    kb = Ontology.connect(db_path)  # embedder defaults to HashingEmbedder
    curator = kb.create_principal("curator@example.com", kind="human", default_capability="admin")

    class Researcher(Concept):
        name: Text
        bio: Text
        h_index: Integer | None = None

    schema = compile_schema("default", 1, Researcher)
    kb.apply_schema(schema, author=curator.id)

    # `confidence` here applies to EVERY assertion on that researcher, so
    # a "low-confidence" researcher genuinely has no qualifying assertion
    # above the threshold below -- min_confidence() keeps an entity if
    # AT LEAST ONE of its assertions clears the bar, not all of them.
    researchers = [
        (
            "ada",
            "Ada Lovelace",
            "Pioneer of computer programming and algorithmic thinking.",
            12,
            1.0,
        ),
        (
            "grace",
            "Grace Hopper",
            "Compiler design and early high-level programming languages.",
            9,
            0.9,
        ),
        (
            "linus",
            "Linus Torvalds",
            "Operating system kernels and distributed version control.",
            15,
            0.4,
        ),
    ]
    for key, name, bio, h_index, confidence in researchers:
        entity = kb.create_entity(concept="Researcher", author=curator.id, natural_key=key)
        kb.assert_literal(
            subject=entity.id,
            predicate="Researcher.name",
            value=name,
            value_type="Text",
            author=curator.id,
            confidence=confidence,
        )
        kb.assert_literal(
            subject=entity.id,
            predicate="Researcher.bio",
            value=bio,
            value_type="Text",
            author=curator.id,
            confidence=confidence,
        )
        kb.assert_literal(
            subject=entity.id,
            predicate="Researcher.h_index",
            value=str(h_index),
            value_type="Integer",
            author=curator.id,
            confidence=confidence,
        )

    # reindex() embeds every entity's Text-typed content -- no write path
    # does this automatically, so it's a deliberate, explicit step.
    n = kb.reindex("Researcher")
    print(f"Indexed {n} researchers for semantic search.\n")

    print("semantic('programming languages'):")
    for r in kb.query("Researcher").semantic("programming languages").limit(2).all():
        print(f"  {r.natural_key}")
    print()

    print(
        "semantic('kernels') + where(h_index__gte=10) — symbolic filter narrows the vector match:"
    )
    # ADR-0020: vector search runs FIRST with an overfetch, then the
    # symbolic .where() filter intersects that set, preserving vector
    # rank order -- not a symbolic prefilter reranked by vector distance.
    results = kb.query("Researcher").semantic("kernels").where(h_index__gte=10).limit(5).all()
    print(f"  {[r.natural_key for r in results]}")
    print()

    print("min_confidence(0.8) — keeps an entity only if AT LEAST ONE assertion clears 0.8:")
    results = kb.query("Researcher").min_confidence(0.8).all()
    print(
        f"  {sorted(r.natural_key for r in results)} (linus is excluded — every one of his assertions was recorded at confidence=0.4)"
    )

    kb.close()
    db_path.unlink()
    print("\n✅ Hybrid search example complete!")


if __name__ == "__main__":
    main()
