# Writing a Plugin

Full source:
[`examples/writing_a_plugin.py`](https://github.com/mattbv/ontolith/blob/main/examples/writing_a_plugin.py)

This tutorial registers and calls a real reference plugin, then covers what
you'd change to write your own.

## Discovery and registration

Plugins are discovered via `importlib.metadata` entry points under the
`"ontolith.plugins"` group — the same mechanism most Python plugin
ecosystems use. `PluginRegistry.register()` requires `admin` capability on
the caller:

```python
from ontolith.plugins.registry import PluginRegistry

loaded = PluginRegistry(kb).register(
    "csv-importer",
    author=admin.id,
    granted_capability="write",  # a CEILING, not a grant of whatever the plugin asks for
)
```

`register()` creates a `service`-kind `Principal` for the plugin (least
privilege by default — `granted_capability` caps what it can do regardless
of what its own manifest requests), and returns a `LoadedPlugin` bundling
that principal's id, a capability-scoped view (`ReadOnlyView`/`WriteView`),
and the plugin instance itself.

## Calling it — and the sandbox underneath

Since ADR-0051, a plugin's protocol entrypoint runs in a **spawned child
process** by default (`isolate=True`), with `capabilities.network`/
`.filesystem` enforced at the OS syscall level on Linux (seccomp). You call
it exactly as you would the real object — the sandboxed round-trip is
transparent:

```python
rows = [{
    "concept": "Person", "natural_key": "grace",
    "predicate": "Person.name", "value": "Grace Hopper", "value_kind": "literal",
}]
report = loaded.instance.import_(rows, loaded.view)
# {"rows_processed": 1, "entities_created": 1, "assertions_proposed": 1}
```

A dataclass return value (like this plugin's own `ImportReport`) crosses an
isolated call as a plain `dict` — the sandbox can only safely round-trip a
restricted set of types, not arbitrary pickled objects (a defense against a
hostile plugin sending back a malicious `__reduce__` payload, found and
fixed in the M4 security review).

!!! warning "Registration-time warnings are expected, not errors"
    Registering a plugin that declares `filesystem=True` or an unenforced
    `network=False` logs real warnings about what actually is/isn't
    enforced on your platform — this project deliberately surfaces gaps
    rather than staying silent about them (see
    [`docs/known-issues.md`](https://github.com/mattbv/ontolith/blob/main/docs/known-issues.md)
    KI-014/KI-106 for the full detail). On macOS/Windows there's no
    OS-level enforcement mechanism at all yet — that's an honestly
    disclosed residual, not a bug in your setup.

## Writing your own

A plugin is one of four protocols
([`ontolith/plugins/ports.py`](https://github.com/mattbv/ontolith/blob/main/src/ontolith/plugins/ports.py)):

| Protocol | Method | Given |
|---|---|---|
| `Importer` | `import_(source, kb)` | a `WriteView` |
| `Exporter` | `export(kb, target)` | a `ReadOnlyView` |
| `Reasoner` | `derive(kb)` | a `WriteView` |
| `Validator` | `validate(assertion, kb)` | a `ValidatorKbView` |

Every plugin class needs a `manifest` class attribute:

```python
from ontolith.plugins.manifest import PluginManifest

class MyImporter:
    manifest = PluginManifest(name="my-importer", version="0.1.0", kind="importer")

    def import_(self, source, kb):
        ...
```

...and a registered entry point in your own package's `pyproject.toml`:

```toml
[project.entry-points."ontolith.plugins"]
my-importer = "my_package.importer:MyImporter"
```

`PluginRegistry` instantiates your plugin class with a **no-argument
constructor** — give it sensible defaults, or a `from_config(...)`
classmethod callers can use before registration if it needs configuration
(the shipped `RequiredFieldsValidator.from_schema()` is a real example of
this pattern).

A `Reasoner`'s derived assertions must still enter through the normal
proposal path — a plugin never bypasses governance, no matter how it
computed the fact.

See
[`src/ontolith/plugins/reference/`](https://github.com/mattbv/ontolith/tree/main/src/ontolith/plugins/reference)
in the repository for four complete, real reference plugins (a CSV
importer, a JSON exporter, an RDF/OWL exporter, and a required-fields
validator) to use as templates.
