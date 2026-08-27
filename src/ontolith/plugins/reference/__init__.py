"""First-party reference plugin implementations (KI-010, ADR-0015).

One plugin per storage posture, registered as real `ontolith.plugins`
entry points in pyproject.toml (not test doubles) — proves plugin
discovery, manifest, capability negotiation, and view-scoped reads/
writes end to end with working code:

- CsvImporter (importer, write): entities/assertions from CSV rows.
- JsonExporter (exporter, read-only): active assertions to JSON.
- RequiredFieldsValidator (validator, read-only): a required-predicate
  business rule, the kind of check the core schema doesn't enforce.
- RdfExporter (exporter, read-only, ADR-0036): schema + active assertions
  as an OWL/RDF document. NOT imported below, unlike the three above —
  its `rdflib` dependency is an optional `interop`-extra, and importing
  this package eagerly imports every plugin listed here (Python always
  initializes a parent package before any of its submodules, including
  when a single plugin is loaded by name via its own entry point) — so
  bundling it in would make `rdflib` a hard requirement for using any
  reference plugin at all. Import it directly:
  `from ontolith.plugins.reference.rdf_exporter import RdfExporter`.
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
