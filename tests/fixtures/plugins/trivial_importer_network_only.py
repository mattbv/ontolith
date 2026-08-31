"""An importer-kind plugin double declaring network-only intent, for
testing PluginRegistry's KI-014 unenforced-capability warning names only
the capability actually requested."""

from ontolith.plugins.manifest import PluginCapabilities, PluginManifest


class TrivialImporterNetworkOnly:
    manifest = PluginManifest(
        name="trivial-importer-net-only",
        version="0.1.0",
        kind="importer",
        capabilities=PluginCapabilities(storage="write", network=True),
    )

    def import_(self, source: object, kb: object) -> None:
        pass
