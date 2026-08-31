"""An importer-kind plugin double declaring filesystem-only intent, for
testing PluginRegistry's KI-014 unenforced-capability warning names only
the capability actually requested."""

from ontolith.plugins.manifest import PluginCapabilities, PluginManifest


class TrivialImporterFilesystemOnly:
    manifest = PluginManifest(
        name="trivial-importer-fs-only",
        version="0.1.0",
        kind="importer",
        capabilities=PluginCapabilities(storage="write", filesystem=True),
    )

    def import_(self, source: object, kb: object) -> None:
        pass
