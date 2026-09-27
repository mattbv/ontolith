"""Schema layer - meta-model, IR, and DSL support.

This module provides schema definition capabilities:
- Internal Representation (IR) - canonical JSON format
- Class DSL - Python class-based schema definition (M3)
- LinkML-aligned YAML front-end (M3)
"""

from ontolith.schema.dsl import (
    JSON,
    URI,
    Boolean,
    Concept,
    Date,
    DateTime,
    Float,
    Integer,
    Property,
    Ref,
    Relation,
    Text,
    compile_schema,
    generate_class_stubs,
)
from ontolith.schema.ir import ConceptDef, PropertyDef, RelationDef, SchemaIR

# `schema.linkml` (to_yaml/from_yaml) and `schema.rdf` (to_owl, ...) are
# deliberately NOT imported here, unlike everything above: both depend on
# packages gated behind the optional `interop` extra (`pyyaml`, `rdflib`
# respectively) — eagerly importing either from this file would make plain
# `import ontolith`/`import ontolith.schema` fail with ModuleNotFoundError
# for anyone who hasn't installed `ontolith[interop]`. Import directly:
# `from ontolith.schema.linkml import from_yaml, to_yaml` /
# `from ontolith.schema.rdf import to_owl`. (Caught during the M4 API-surface
# freeze audit: an earlier pass of that same audit briefly added `from_yaml`/
# `to_yaml` here without checking this, verified broken, and reverted before
# merge — see ADR-0019's own Update section for the record.)

__all__ = [
    "SchemaIR",
    "ConceptDef",
    "PropertyDef",
    "RelationDef",
    "Concept",
    "Relation",
    "Property",
    "Ref",
    "Text",
    "Integer",
    "Float",
    "Boolean",
    "Date",
    "DateTime",
    "URI",
    "JSON",
    "compile_schema",
    "generate_class_stubs",
]
