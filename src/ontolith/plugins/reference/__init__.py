"""First-party reference plugin implementations (KI-010, ADR-0015).

One plugin per storage posture, registered as real `ontolith.plugins`
entry points in pyproject.toml (not test doubles) — proves plugin
discovery, manifest, capability negotiation, and view-scoped reads/
writes end to end with working code:

- CsvImporter (importer, write): entities/assertions from CSV rows.
- JsonExporter (exporter, read-only): active assertions to JSON.
- RequiredFieldsValidator (validator, read-only): a required-predicate
  business rule, the kind of check the core schema doesn't enforce.
"""

from ontolith.plugins.reference.csv_importer import CsvImporter, ImportReport
from ontolith.plugins.reference.json_exporter import ExportReport, JsonExporter
from ontolith.plugins.reference.required_fields_validator import RequiredFieldsValidator

__all__ = [
    "CsvImporter",
    "ImportReport",
    "JsonExporter",
    "ExportReport",
    "RequiredFieldsValidator",
]
