"""Schema layer - meta-model, IR, and DSL support.

This module provides schema definition capabilities:
- Internal Representation (IR) - canonical JSON format
- Class DSL - Python class-based schema definition (M1)
- YAML front-end (M2)
"""

from ontolith.schema.ir import ConceptDef, PropertyDef, RelationDef, SchemaIR

__all__ = ["SchemaIR", "ConceptDef", "PropertyDef", "RelationDef"]
