"""Process isolation for plugin execution (ADR-0051, KI-014).

Each plugin's protocol entrypoint runs in a freshly spawned child process
(`runner.py`), with its `kb` view and any other live argument proxied back
to the parent over one shared pipe (`protocol.py`, `remote_view.py`).
Network/filesystem denial is enforced at the OS syscall level on Linux
(`enforcement.py`); elsewhere, the process/IPC boundary alone is the
isolation a plugin gets, honestly documented as such.

`plugins/registry.py` is this package's only intended caller — import
directly from the submodule that defines what you need (e.g.
`from ontolith.plugins.sandbox.runner import IsolatedPluginProxy`), matching
this project's existing convention elsewhere in `plugins/` (`registry.py`
imports `ReadOnlyView`/`WriteView` from `plugins.views`, not a package-level
re-export). Deliberately no re-exports here: `runner.py` itself imports
sibling submodules in this package, and re-exporting through `__init__.py`
would create a real circular import at module-init time.
"""
