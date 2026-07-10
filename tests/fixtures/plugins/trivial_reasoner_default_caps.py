"""A write-capable-kind plugin double that (mistakenly, or by omission)
declares only the default PluginCapabilities (storage='read')."""

from ontolith.plugins.manifest import PluginManifest


class TrivialReasonerDefaultCaps:
    manifest = PluginManifest(name="trivial-reasoner-default", version="0.1.0", kind="reasoner")

    def derive(self, kb: object) -> None:
        pass
