"""JSON exporter reference plugin (SPEC §13, ADR-0015, KI-010).

Demonstrates the Exporter protocol (plugins/ports.py) against a real
ReadOnlyView: writes every currently-active assertion as a JSON array,
one object per assertion, serialized via Assertion.model_dump(mode=
"json") rather than a hand-picked field subset — this stays correct
automatically if the Assertion model gains/renames fields, instead of
silently dropping data.

`target` accepts a path (str/Path, opened for writing) or any writable
file-like object (e.g. io.StringIO for tests) — checked structurally
via hasattr(target, "write") rather than an isinstance allowlist, so
any text-mode writable works.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from ontolith.plugins.manifest import PluginManifest
from ontolith.plugins.views import ReadOnlyView


@dataclass(frozen=True)
class ExportReport:
    """Summary of a completed export (SPEC §13's ExportReport)."""

    assertions_written: int


class JsonExporter:
    """Reference Exporter plugin: serializes active assertions to JSON."""

    manifest = PluginManifest(name="json-exporter", version="0.1.0", kind="exporter")

    def export(self, kb: ReadOnlyView, target: object) -> object:
        """Write every active assertion as a JSON array to `target`."""
        records = [assertion.model_dump(mode="json") for assertion in kb.assertions()]

        if hasattr(target, "write"):
            json.dump(records, target)
        elif isinstance(target, (str, Path)):
            with open(target, "w", encoding="utf-8") as fh:
                json.dump(records, fh)
        else:
            raise TypeError(f"Unsupported JSON export target type: {type(target).__name__}")

        return ExportReport(assertions_written=len(records))


__all__ = ["JsonExporter", "ExportReport"]
