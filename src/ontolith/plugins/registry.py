"""Plugin discovery and capability-scoped loading (ADR-0015).

PluginRegistry.register() is the single entry point for turning a discovered
plugin into a LoadedPlugin bound to a capability-scoped view. It requires an
admin-capability author (mirrors Ontology.issue_token's gate, ADR-0014) —
loading a plugin creates a standing in-process actor, a higher-stakes action
than the plugin's own requested capability.
"""

from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, get_args

from ontolith.core.errors import PluginError, ValidationError
from ontolith.identity.principal import min_capability
from ontolith.ontology import Ontology
from ontolith.plugins.manifest import PluginKind, PluginManifest
from ontolith.plugins.views import ReadOnlyView, WriteView

_ENTRY_POINT_GROUP = "ontolith.plugins"
_PLUGIN_METADATA_MARKER = "ontolith_plugin"

# Write-capable kinds are the explicit, authoritative allow-list — a new
# PluginKind not added here defaults to read-only (the safe direction for a
# security-relevant partition: forgetting to grant write is safe, forgetting
# to restrict it is not).
_WRITE_CAPABLE_KINDS = frozenset({"importer", "reasoner", "connector"})
_READ_ONLY_KINDS = frozenset(get_args(PluginKind)) - _WRITE_CAPABLE_KINDS


@dataclass(frozen=True)
class LoadedPlugin:
    """A discovered plugin bound to a capability-scoped view.

    Attributes:
        manifest: The plugin's declared manifest.
        principal_id: The service-Principal id this plugin acts as.
        view: ReadOnlyView or WriteView scoped to principal_id, per the
            plugin's effective capability.
        instance: The instantiated plugin object.
    """

    manifest: PluginManifest
    principal_id: str
    view: ReadOnlyView
    instance: object


class PluginRegistry:
    """Discovers plugins via entry points and loads them with least privilege."""

    def __init__(self, kb: Ontology) -> None:
        self._kb = kb

    def register(
        self,
        entry_point_name: str,
        *,
        author: str,
        granted_capability: str = "propose",
    ) -> LoadedPlugin:
        """Discover, load, and sandbox a plugin by entry-point name.

        Args:
            entry_point_name: Name of the entry point under the
                "ontolith.plugins" group.
            author: Principal ID performing the registration — must hold
                `admin` capability.
            granted_capability: Ceiling on what storage capability this
                plugin may be granted, regardless of what its manifest
                requests. Defaults to "propose" (least privilege) —
                further capped to "read" for read-only plugin kinds.

        Returns:
            LoadedPlugin bound to a capability-scoped view.

        Raises:
            AuthError: author is not a known principal
            CapabilityError: author lacks admin capability
            ValidationError: granted_capability is not a known capability level
            PluginError: entry point not found or ambiguous, manifest
                missing/invalid, a write-capable plugin's effective
                capability resolved to "read", or the plugin's name is
                already occupied by an unrelated principal
        """
        self._kb.require_admin(author)

        plugin_obj = self._load_entry_point(entry_point_name)
        manifest = self._load_manifest(plugin_obj, entry_point_name)
        effective_capability = self._effective_capability(manifest, granted_capability)

        if manifest.kind in _WRITE_CAPABLE_KINDS and effective_capability == "read":
            raise PluginError(
                f"Plugin {manifest.name!r} (kind={manifest.kind!r}) needs at least "
                f"'propose' storage capability to function, but resolved to 'read' "
                f"(manifest requested {manifest.capabilities.storage!r}, granted "
                f"{granted_capability!r}). Increase capabilities.storage in the "
                "manifest or the granted_capability passed to register()."
            )

        principal_id = self._ensure_principal(manifest, effective_capability)
        view = self._build_view(principal_id, effective_capability)

        return LoadedPlugin(
            manifest=manifest,
            principal_id=principal_id,
            view=view,
            instance=plugin_obj,
        )

    def _load_entry_point(self, name: str) -> Any:
        """Resolve and instantiate the plugin class registered under `name`."""
        matches = [ep for ep in entry_points(group=_ENTRY_POINT_GROUP) if ep.name == name]
        if not matches:
            raise PluginError(f"Plugin entry point not found: {name!r}")
        if len(matches) > 1:
            raise PluginError(
                f"Ambiguous plugin entry point {name!r}: {len(matches)} distributions "
                "register this name under the 'ontolith.plugins' group"
            )
        try:
            plugin_class = matches[0].load()
        except Exception as exc:
            raise PluginError(f"Failed to load plugin {name!r}: {exc}") from exc
        try:
            return plugin_class()
        except Exception as exc:
            raise PluginError(f"Failed to instantiate plugin {name!r}: {exc}") from exc

    def _load_manifest(self, plugin_obj: Any, entry_point_name: str) -> PluginManifest:
        """Validate and return `plugin_obj`'s declared manifest."""
        manifest = getattr(plugin_obj, "manifest", None)
        if manifest is None:
            raise PluginError(f"Plugin {entry_point_name!r} has no `manifest` attribute")
        if not isinstance(manifest, PluginManifest):
            raise PluginError(
                f"Plugin {entry_point_name!r} manifest is not a PluginManifest instance"
            )
        return manifest

    def _effective_capability(self, manifest: PluginManifest, granted: str) -> str:
        """min(requested, granted), then hard-capped to 'read' for read-only
        kinds — a ceiling override, not another min() call, since there is
        no lattice level below 'read' to min against."""
        try:
            capped = min_capability(manifest.capabilities.storage, granted)
        except KeyError as exc:
            raise ValidationError(f"Unknown capability level: {granted!r}") from exc
        if manifest.kind in _READ_ONLY_KINDS:
            return "read"
        return capped

    def _ensure_principal(self, manifest: PluginManifest, effective_capability: str) -> str:
        """Get or create the service principal bound to this plugin, and return its ID."""
        existing = self._kb.get_principal(manifest.name)
        if existing is None:
            principal = self._kb.create_principal(
                manifest.name,
                kind="service",
                auth_method="workload",
                default_capability=effective_capability,
                metadata={_PLUGIN_METADATA_MARKER: True},
            )
            return principal.id

        if existing.kind != "service" or not existing.metadata.get(_PLUGIN_METADATA_MARKER):
            raise PluginError(
                f"Principal {manifest.name!r} already exists and is not a plugin "
                "principal registered by PluginRegistry — refusing to bind to it, "
                "to avoid conflating two unrelated identities under one name"
            )
        if existing.default_capability != effective_capability:
            raise PluginError(
                f"Plugin {manifest.name!r} is already registered with capability "
                f"{existing.default_capability!r}, which differs from the newly computed "
                f"{effective_capability!r}. Principals are immutable — revoke and recreate "
                "the plugin principal to change its granted capability."
            )
        return existing.id

    def _build_view(self, principal_id: str, effective_capability: str) -> ReadOnlyView:
        """View type is fully determined by effective capability — the
        kind/read-only ceiling is already folded in by _effective_capability,
        so no separate kind check is needed here."""
        if effective_capability == "read":
            return ReadOnlyView(self._kb, principal_id)
        return WriteView(self._kb, principal_id)


__all__ = ["LoadedPlugin", "PluginRegistry"]
