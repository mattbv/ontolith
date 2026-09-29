#!/usr/bin/env python3
"""Writing & registering a plugin example.

This example demonstrates:
- Registering a real reference plugin (the CSV importer) via
  PluginRegistry, discovered through the "ontolith.plugins" entry-point
  group (SPEC §13, ADR-0015)
- Least-privilege by default: `isolate=True` runs the plugin's
  entrypoint in a sandboxed child process (ADR-0051), with network/
  filesystem enforced via seccomp on Linux
- Calling the plugin through its sandboxed proxy exactly as you would
  the real object -- the subprocess round-trip is transparent
- The shape of the Importer protocol itself, for writing your own
"""

import tempfile
from pathlib import Path

from ontolith import Ontology
from ontolith.plugins.registry import PluginRegistry


def main() -> None:
    """Run the plugin registration example."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(f.name)
    f.close()
    print("🔌 Ontolith Plugin Example\n")

    kb = Ontology.connect(db_path)
    admin = kb.create_principal("admin@example.com", kind="human", default_capability="admin")

    # PluginRegistry.register() requires admin capability on the caller,
    # discovers the plugin by its "ontolith.plugins" entry-point name
    # (declared in the plugin package's own pyproject.toml), and registers
    # it as a service Principal with a capped default_capability.
    # NOTE: registering a plugin that declares filesystem=True/network=False
    # logs real warnings below (via kb.observability's default logging
    # sink) about what actually is/isn't enforced on this platform -- this
    # project deliberately surfaces those rather than staying silent about
    # a security posture gap (ADR-0051, KI-014, KI-106). Expected here.
    print("Registering the 'csv-importer' reference plugin...")
    loaded = PluginRegistry(kb).register(
        "csv-importer",
        author=admin.id,
        granted_capability="write",  # ceiling on what this plugin may do
    )
    print(f"✓ Registered as principal {loaded.principal_id!r}")
    print(f"  Manifest: {loaded.manifest.name} v{loaded.manifest.version}")
    print(f"  Sandboxed: {loaded.instance.__class__.__name__} wraps {loaded.instance.plugin_class}")
    print()

    # One CSV "row" = one assertion. Entities are created (deduplicated
    # by natural_key) before assertions are proposed, so a ref row can
    # point at a natural_key appearing later in the same batch.
    rows = [
        {
            "concept": "Person",
            "natural_key": "grace",
            "predicate": "Person.name",
            "value": "Grace Hopper",
            "value_kind": "literal",
        },
    ]

    # loaded.instance.import_(...) is called exactly as you'd call the
    # real CsvImporter object -- isolate=True (the default) means this
    # call actually crosses into a spawned child process and back, but
    # that round-trip is transparent to the caller.
    print("Importing rows through the sandboxed plugin...")
    report = loaded.instance.import_(rows, loaded.view)
    # A dataclass return value crosses an isolated call as a plain dict
    # (the sandbox can't pickle arbitrary objects back safely).
    print(
        f"✓ {report['entities_created']} entities created, {report['assertions_proposed']} assertions proposed"
    )
    print()

    grace_facts = kb.assertions(predicate="Person.name")
    for fact in grace_facts:
        print(f"  {fact.subject}: {fact.predicate} = {fact.value!r} (author={fact.author})")

    kb.close()
    db_path.unlink()
    print("\n✅ Plugin example complete!")
    print(
        "\nTo write your own: implement the Importer/Exporter/Reasoner/"
        "Validator/Connector protocol (src/ontolith/plugins/ports.py), "
        "give your plugin class a manifest = PluginManifest(name=..., "
        "version=..., kind=..., capabilities=PluginCapabilities(storage="
        "'propose')) class attribute -- storage defaults to 'read', so a "
        "writing plugin must declare at least 'propose' or registration "
        "refuses it -- and register an entry point under the "
        "'ontolith.plugins' group in your package's pyproject.toml."
    )


if __name__ == "__main__":
    main()
