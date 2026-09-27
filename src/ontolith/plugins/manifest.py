"""Plugin manifest schema (ADR-0015, ADR-0051).

A plugin declares its identity, kind, and requested capabilities via a
PluginManifest. `capabilities.storage` is enforced by PluginRegistry
(ADR-0015). `capabilities.network`/`.filesystem` are enforced at the OS
syscall level when a plugin is registered with `isolate=True` (the
default) on Linux (ADR-0051, via seccomp); on other platforms, or when a
plugin is registered with `isolate=False`, they remain declared but not
enforced — see ADR-0051's Consequences section for the platform-by-platform
breakdown.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PluginKind = Literal["importer", "exporter", "reasoner", "validator", "connector"]
"""Plugin kinds covered by capability isolation.

Deliberately excludes StorageBackend/AuthProvider/PolicyStrategy/Embedder
plugins: those are infrastructure-extension points the framework calls INTO
(e.g. a StorageBackend plugin IS the persistence substrate underneath
WriteView, not a principal-scoped actor calling through it) — they don't fit
the capability-scoped-view model this module implements. See ADR-0015
"Plugin kinds in scope" and ADR-0020 (Embedder specifically: its Protocol
takes no `kb` view parameter at all, so it structurally cannot participate
in this module's ReadOnlyView/WriteView dispatch; it lives in
core/embedder.py, injected into Ontology directly like Clock/IdProvider).
"""


class PluginCapabilities(BaseModel):
    """Capabilities a plugin requests.

    Attributes:
        storage: Requested ceiling on the read/propose/write lattice
            (identity.principal._CAPABILITY_ORDER). Enforced by
            PluginRegistry — the plugin's effective capability is
            min(this, the capability granted at registration), further
            capped to "read" for read-only plugin kinds regardless of what
            is requested here.
        network: Declares intent to make network calls. Enforced via OS
            syscall denial when `False` and the plugin runs isolated on
            Linux (ADR-0051); declared but not enforced with
            `isolate=False` or on a non-Linux platform.
        filesystem: Declares intent to touch the filesystem. Same
            enforcement story as `network` (ADR-0051).
    """

    storage: Literal["read", "propose", "write"] = "read"
    network: bool = False
    filesystem: bool = False

    model_config = ConfigDict(frozen=True)


class PluginManifest(BaseModel):
    """A plugin's declared identity and capability request.

    Attributes:
        name: Plugin identifier. Becomes the id of the service-kind
            Principal PluginRegistry creates/reuses for this plugin.
        version: Plugin version string.
        kind: What kind of plugin this is — determines the ceiling on
            effective storage capability (read-only kinds are capped at
            "read" regardless of requested/granted capability) and which
            view type (ReadOnlyView vs WriteView) the plugin receives.
        capabilities: Requested capabilities (see PluginCapabilities).
    """

    name: str = Field(min_length=1)
    version: str
    kind: PluginKind
    capabilities: PluginCapabilities = Field(default_factory=PluginCapabilities)

    model_config = ConfigDict(frozen=True)


__all__ = ["PluginKind", "PluginCapabilities", "PluginManifest"]
