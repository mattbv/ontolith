"""A plugin double whose `.manifest` is not a PluginManifest instance."""


class BrokenBadManifest:
    manifest = {"name": "not-a-real-manifest"}
