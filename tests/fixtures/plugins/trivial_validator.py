"""A trivial validator-kind plugin double for registry tests.

Deliberately requests storage="write" in its own manifest — validator is a
read-only kind, so PluginRegistry must hard-cap its effective capability at
"read" regardless of this over-request.
"""

from ontolith.plugins.manifest import PluginCapabilities, PluginManifest


class TrivialValidator:
    manifest = PluginManifest(
        name="trivial-validator",
        version="0.1.0",
        kind="validator",
        capabilities=PluginCapabilities(storage="write"),
    )

    def validate(self, assertion: object, kb: object) -> list[str]:
        return []
