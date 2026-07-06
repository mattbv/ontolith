"""Golden test: classes -> IR -> YAML -> IR -> classes idempotence (Implementation
Plan §6, SPEC §6.1's full bidirectional codegen guarantee).

Depends on the class DSL (`ontolith.schema.dsl`, PR #11 / m3/class-dsl), which is
not yet merged to main as of this branch. Skipped until that lands; fill in the
body below with a real assertion once `ontolith.schema.dsl` is importable here.
"""

import pytest

pytest.importorskip(
    "ontolith.schema.dsl",
    reason="Requires the class DSL from m3/class-dsl (PR #11), not yet merged",
)


def test_classes_ir_yaml_ir_classes_roundtrip() -> None:
    from ontolith.schema.dsl import Concept, Date, Ref, Relation, Text, compile_schema

    from ontolith.schema.linkml import from_yaml, to_yaml

    class Organization(Concept):
        name: Text

    class Person(Concept):
        name: Text
        born: Date | None = None
        employer: Ref["Organization"] = Relation(inverse="employees", temporality="time_varying")

    schema = compile_schema("default", 1, Person, Organization)

    reconstructed = from_yaml(to_yaml(schema))
    assert reconstructed.concepts == schema.concepts
