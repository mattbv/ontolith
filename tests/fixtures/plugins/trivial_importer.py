"""A trivial importer-kind plugin double for registry tests."""

from ontolith.plugins.manifest import PluginCapabilities, PluginManifest


class TrivialImporter:
    manifest = PluginManifest(
        name="trivial-importer",
        version="0.1.0",
        kind="importer",
        capabilities=PluginCapabilities(storage="write"),
    )

    def import_(self, source: object, kb: object) -> None:
        pass
