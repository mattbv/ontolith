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
