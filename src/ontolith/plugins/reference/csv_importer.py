"""CSV importer reference plugin (SPEC §13, ADR-0015, KI-010).

Demonstrates the Importer protocol (plugins/ports.py) against a real
WriteView: one CSV row = one assertion. Two passes over the rows —
entities are created (deduplicated by natural_key) before any assertion
is proposed, so ref rows can point at natural_keys appearing later in
the file.

Row columns: concept, natural_key, predicate, value, value_kind,
value_type (optional, defaults to "Text"), confidence (optional).
value_kind is "literal" or "ref". For "ref" rows, `value` holds the
TARGET's natural_key (not a generated entity id — the CSV author can't
know that in advance), resolved against natural_keys seen as a row
subject elsewhere in the same file.

Known simplification (deliberate, not a defect): natural_keys must be
unique across the whole file, not just per-concept, since resolution
uses a single flat map. Ontology.create_entity has no create-or-reuse-
by-natural-key lookup of its own (confirmed absent from StorageBackend
and QueryBuilder), so this plugin does its own in-memory deduplication
within a single import_() call; it does not detect duplicates across
separate import_() calls (that would require an entity read of the
whole KB, out of scope for a reference plugin).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ontolith.core.errors import ValidationError
from ontolith.plugins.manifest import PluginCapabilities, PluginManifest
from ontolith.plugins.views import WriteView

_REQUIRED_COLUMNS = ("concept", "natural_key", "predicate", "value", "value_kind")


@dataclass(frozen=True)
class ImportReport:
    """Summary of a completed CSV import (SPEC §13's ImportReport)."""

    rows_processed: int
    entities_created: int
    assertions_proposed: int


class CsvImporter:
    """Reference Importer plugin: creates entities/assertions from CSV rows."""

    manifest = PluginManifest(
        name="csv-importer",
        version="0.1.0",
        kind="importer",
        # storage="write", not "propose": import_() itself only ever calls
        # propose-level WriteView methods, but ThresholdPolicy only
        # auto-accepts a service principal at "write" (propose-capability
        # service principals go to RequireReview per-row, see govern/
        # policy.py). Declaring "propose" here would cap every operator's
        # grant at a ceiling that can never auto-accept a bulk import - the
        # whole point of a trusted admin registering a bulk CsvImporter.
        capabilities=PluginCapabilities(storage="write", filesystem=True),
    )

    def import_(self, source: object, kb: WriteView) -> ImportReport:
        """Import CSV rows from a file path or an iterable of row mappings."""
        rows = self._read_rows(source)

        key_map: dict[str, str] = {}
        entities_created = 0
        for index, row in enumerate(rows, start=1):
            self._require_columns(row, index)
            natural_key = row["natural_key"]
            if natural_key not in key_map:
                entity = kb.create_entity(row["concept"], natural_key=natural_key)
                key_map[natural_key] = entity.id
                entities_created += 1

        assertions_proposed = 0
        for index, row in enumerate(rows, start=1):
            subject_id = key_map[row["natural_key"]]
            confidence = self._parse_confidence(row.get("confidence"), index)
            value_kind = row["value_kind"]
            if value_kind == "literal":
                value_type = row.get("value_type") or "Text"
                kb.propose(
                    subject_id,
                    row["predicate"],
                    row["value"],
                    value_type,
                    confidence=confidence,
                )
            elif value_kind == "ref":
                target_key = row["value"]
                if target_key not in key_map:
                    raise ValidationError(
                        f"Row {index}: ref target natural_key {target_key!r} was never "
                        "seen as a subject in this file",
                        detail={"row": index, "target_natural_key": target_key},
                    )
                kb.propose_ref(
                    subject_id,
                    row["predicate"],
                    key_map[target_key],
                    confidence=confidence,
                )
            else:
                raise ValidationError(
                    f"Row {index}: value_kind must be 'literal' or 'ref', got {value_kind!r}",
                    detail={"row": index, "value_kind": value_kind},
                )
            assertions_proposed += 1

        return ImportReport(
            rows_processed=len(rows),
            entities_created=entities_created,
            assertions_proposed=assertions_proposed,
        )

    @staticmethod
    def _read_rows(source: object) -> list[dict[str, str]]:
        """Load `source` (a path or an iterable of row mappings) into row dicts."""
        import csv

        if isinstance(source, (str, Path)):
            with open(source, newline="", encoding="utf-8") as fh:
                return [dict(row) for row in csv.DictReader(fh)]
        if isinstance(source, Iterable):
            return [dict(row) for row in source]
        raise ValidationError(f"Unsupported CSV import source type: {type(source).__name__}")

    @staticmethod
    def _require_columns(row: dict[str, str], index: int) -> None:
        """Raise ValidationError if `row` is missing any required CSV column."""
        missing = [column for column in _REQUIRED_COLUMNS if not row.get(column)]
        if missing:
            raise ValidationError(
                f"Row {index}: missing required column(s): {', '.join(missing)}",
                detail={"row": index, "missing_columns": missing},
            )

    @staticmethod
    def _parse_confidence(raw: str | None, index: int) -> float | None:
        """Parse the optional confidence column, or None if blank."""
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except ValueError as exc:
            raise ValidationError(
                f"Row {index}: confidence {raw!r} is not a valid float",
                detail={"row": index, "confidence": raw},
            ) from exc


__all__ = ["CsvImporter", "ImportReport"]
