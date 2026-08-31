"""An importer-kind plugin double declaring network/filesystem intent, for
testing PluginRegistry's KI-014 unenforced-capability warning."""

from ontolith.plugins.manifest import PluginCapabilities, PluginManifest


class TrivialImporterNetworkAndFilesystem:
    manifest = PluginManifest(
        name="trivial-importer-net-fs",
        version="0.1.0",
        kind="importer",
        capabilities=PluginCapabilities(storage="write", network=True, filesystem=True),
    )

    def import_(self, source: object, kb: object) -> None:
        pass
