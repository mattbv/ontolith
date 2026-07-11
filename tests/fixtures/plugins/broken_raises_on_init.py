"""A plugin double whose constructor raises."""

from ontolith.plugins.manifest import PluginManifest


class BrokenRaisesOnInit:
    manifest = PluginManifest(name="broken-raises-on-init", version="0.1.0", kind="importer")

    def __init__(self) -> None:
        raise RuntimeError("plugin construction deliberately fails")
