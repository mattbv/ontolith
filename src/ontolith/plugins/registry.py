"""Plugin discovery and capability-scoped loading (ADR-0015).

PluginRegistry.register() is the single entry point for turning a discovered
plugin into a LoadedPlugin bound to a capability-scoped view. It requires an
admin-capability author (mirrors Ontology.issue_token's gate, ADR-0014) —
loading a plugin creates a standing in-process actor, a higher-stakes action
than the plugin's own requested capability.
"""

import logging
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, get_args

from ontolith.core.errors import PluginError, ValidationError
from ontolith.identity.principal import min_capability
from ontolith.ontology import Ontology
from ontolith.plugins.manifest import PluginKind, PluginManifest
from ontolith.plugins.sandbox import enforcement
from ontolith.plugins.sandbox.runner import IsolatedPluginProxy
from ontolith.plugins.views import ReadOnlyView, WriteView

_ENTRY_POINT_GROUP = "ontolith.plugins"
_PLUGIN_METADATA_MARKER = "ontolith_plugin"
# Recorded on a plugin principal's own metadata so a later register() call
# under the same manifest.name but a DIFFERENT entry point is refused
# rather than silently rebinding to the existing principal's identity
# (security review finding, M4 Workstream 7) — see _ensure_principal.
_ENTRY_POINT_METADATA_KEY = "ontolith_plugin_entry_point"

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
        instance: The plugin's one protocol entrypoint, callable the same
            way the real object would be (`instance.import_(...)`,
            `instance.export(...)`, etc.). When registered with
            `isolate=True` (the default), this is an `IsolatedPluginProxy`
            (ADR-0051) that runs the call in a sandboxed child process —
            not the real, directly-instantiated plugin object, so
            `isinstance(instance, SomePluginClass)` no longer holds; use
            `instance.plugin_class` for that check instead. With
            `isolate=False`, this is the real, unsandboxed plugin instance,
            exactly as before ADR-0051.
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
        isolate: bool = True,
        require_enforcement: bool = False,
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
            isolate: Run the plugin's protocol entrypoint in a sandboxed
                child process (ADR-0051), not this process. Defaults to
                `True` — deny-by-default, the same posture default
                temporality/MCP's read-propose-flag-only surface/capability
                floors already take elsewhere in this project. Pass
                `False` only for a plugin whose `source`/`target` argument
                genuinely can't cross a process boundary (an unpicklable,
                non-`.write`-shaped live object) or a fully trusted
                first-party plugin where the per-call subprocess overhead
                isn't worth paying — this restores the pre-ADR-0051
                behavior exactly: the real, unsandboxed plugin instance,
                network/filesystem fully unenforced regardless of platform.
            require_enforcement: Refuse to register (`PluginError`) rather
                than merely warn if OS-level capability enforcement can't
                even be attempted for this registration — `isolate=False`,
                or `enforcement.enforcement_available()` is False on this
                host/platform (currently: not Linux, or `pyseccomp`/
                libseccomp missing). Defaults to `False` (today's behavior:
                warn and proceed) since flipping the default would refuse
                every registration on macOS/Windows outright — security
                review finding, M4 Workstream 7: an operator who needs a
                real guarantee that `capabilities.network=False`/
                `filesystem=False` is enforced, not just declared, had no
                way to ask for one; the registration-time warning
                (`_warn_if_unenforced_capabilities_requested`) is the only
                signal otherwise, and it's advisory only. This same flag is
                also threaded through to every isolated *call*
                (`sandbox.runner.run_isolated`'s own `require_enforcement`
                parameter): with `require_enforcement=True`, a call whose
                enforcement was available in principle at registration but
                whose specific attempt to install the filter fails at call
                time (e.g. a container's own outer seccomp profile blocks
                it) also refuses, rather than merely warning as it does
                with the default `False` — this call-time check is the
                narrower residual registration alone can't predict, not a
                second, independent guarantee (round-2 review finding: an
                earlier version of this docstring wrongly implied the
                call-time check fires even with `require_enforcement=
                False`, which the registered behavior — and this flag's own
                default-preserves-today's-behavior framing above — both
                contradict).

        Returns:
            LoadedPlugin bound to a capability-scoped view.

        Raises:
            AuthError: author is not a known principal
            CapabilityError: author lacks admin capability
            ValidationError: granted_capability is not a known capability level
            PluginError: entry point not found or ambiguous, manifest
                missing/invalid, a write-capable plugin's effective
                capability resolved to "read", the plugin's name is
                already occupied by an unrelated principal, or
                require_enforcement=True but OS-level enforcement can't be
                attempted for this registration at all

        Note:
            Logs a `logging.WARNING` (KI-014) if the plugin's manifest
            declares `capabilities.network`/`.filesystem` and either
            `isolate=False` was passed or no OS-level enforcement is
            available for this platform/capability (ADR-0051) — the
            declaration alone doesn't restrict anything in that case. Only
            logged on successful registration (the plugin actually becomes
            a standing in-process actor), not on a failed attempt.

            Records a `register_plugin` `AdminEvent` on successful
            registration (KI-060), same "only on success" timing as the
            warning above. If this is the plugin's first registration
            (no existing principal), `_ensure_principal`'s own
            `create_principal(..., author=author)` call also records a
            separate `create_principal` event — two events for one
            `register()` call in that case, one for each distinct action
            that actually happened.
        """
        self._kb.require_admin(author)

        if require_enforcement and (not isolate or not enforcement.enforcement_available()):
            reason = (
                "isolate=False was passed for this registration"
                if not isolate
                else "no OS-level enforcement is available on this host/platform "
                "(not Linux, or pyseccomp/libseccomp is missing)"
            )
            raise PluginError(
                f"require_enforcement=True but OS-level capability enforcement cannot be "
                f"attempted for this registration ({reason}) — refusing to register rather "
                "than silently proceed with capabilities.network/.filesystem unenforced "
                "(ADR-0051)."
            )

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

        principal_id = self._ensure_principal(
            manifest, effective_capability, author, entry_point_name
        )
        view = self._build_view(principal_id, effective_capability)
        # Warn/record only once registration actually succeeds (review
        # finding for the warning, same reasoning extends to the audit
        # event, KI-060): this plugin is now live in-process with
        # unenforced network/filesystem access, which is the claim the
        # warning makes — a registration attempt that fails before this
        # point never becomes a standing in-process actor at all, and
        # shouldn't be recorded as though one was created.
        self._warn_if_unenforced_capabilities_requested(manifest, isolate)
        self._kb.record_admin_event(author, "register_plugin", manifest.name)

        instance: object = (
            IsolatedPluginProxy(
                entry_point_name,
                manifest,
                type(plugin_obj),
                self._kb.observability,
                require_enforcement=require_enforcement,
            )
            if isolate
            else plugin_obj
        )
        return LoadedPlugin(
            manifest=manifest,
            principal_id=principal_id,
            view=view,
            instance=instance,
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

    def _warn_if_unenforced_capabilities_requested(
        self, manifest: PluginManifest, isolate: bool
    ) -> None:
        """Log a visible warning for either of two distinct ways a plugin's
        declared network/filesystem intent doesn't mean what the manifest
        API visually implies (ADR-0015, ADR-0051):

        1. **Declared `True` (requesting access)**: this is an *allow*, and
           this project never gates it with a ceiling the way
           `granted_capability` gates storage — there is no "grant" step
           for network/filesystem to withhold. `apply_capability_enforcement`
           (`sandbox/enforcement.py`) never restricts *this specific*
           declared-`True` capability, on any platform, with or without
           `isolate` — network syscalls stay allowed if `network=True`,
           filesystem syscalls stay allowed if `filesystem=True` (a
           `ptrace`/`process_vm_*`/`pidfd_*` denial is installed
           unconditionally regardless of either declaration, but that's an
           unrelated, always-on floor, not a consequence of what's declared
           here — see `enforcement.py`'s own comment). Always warn.
           `filesystem=True` specifically also subsumes `granted_capability`
           itself, not just network/filesystem's own advisory posture: a
           plugin that can touch the filesystem can open the KB's on-disk
           file directly and write to it, bypassing proposal/policy/audit
           entirely, regardless of how low its granted storage capability
           is (security review finding, M4 Workstream 7 — filed as KI-106,
           the structural fix — parent-opened handles for import sources
           too, mirroring `RemoteWritable`'s existing treatment of export
           targets — is out of scope for this pass; disclosed in the
           warning below instead).
        2. **Declared `False` (the default — requesting denial)** *and* this
           registration won't actually get it: `isolate=False` was passed,
           or no OS-level enforcement is available on this host/platform
           (`enforcement.enforcement_available()`). The manifest's `False`
           looks like a real guarantee; for this registration it isn't.
           Warn in that case too — silently saying nothing here would be
           the exact false sense of enforcement this warning exists to
           prevent (security review finding: the pre-fix version only
           checked case 1, so a `filesystem=False` plugin on macOS/Windows,
           or with `isolate=False` anywhere, registered with no signal
           at all).

        Either way, there is no separate "grant" lever for network/
        filesystem the way there is for storage — the plugin author
        declares intent in the manifest, and the operator's only lever is
        whether to register the plugin at all. See ADR-0015's and
        ADR-0051's Consequences for the full statement of what is and
        isn't defended against.
        """
        enforced_for_real = isolate and enforcement.enforcement_available()

        declared_true = [
            name
            for name, requested in (
                ("network", manifest.capabilities.network),
                ("filesystem", manifest.capabilities.filesystem),
            )
            if requested
        ]
        if declared_true:
            verb = "is" if len(declared_true) == 1 else "are"
            filesystem_note = (
                " filesystem=True also subsumes capabilities.storage's own ceiling: a plugin "
                "that can touch the filesystem can open the KB's own on-disk file directly "
                "(e.g. sqlite3.connect(path)) and write to it with no proposal, no policy "
                "evaluation, and no audit event — storage='read'/'propose' is not a real limit "
                "on a plugin that also has filesystem=True (security review finding, M4 "
                "Workstream 7; see KI-106)."
                if "filesystem" in declared_true
                else ""
            )
            self._kb.observability.log(
                logging.WARNING,
                f"Plugin {manifest.name!r} declares capabilities.{'/'.join(declared_true)}=True "
                f"— {verb} an allow, never enforced as a ceiling: this plugin can make network "
                "calls / touch the filesystem regardless of isolate or platform "
                f"(ADR-0015, ADR-0051, KI-014).{filesystem_note}",
                plugin=manifest.name,
                declared_true=declared_true,
                isolate=isolate,
            )

        if enforced_for_real:
            return
        declared_false_unenforced = [
            name
            for name, requested in (
                ("network", manifest.capabilities.network),
                ("filesystem", manifest.capabilities.filesystem),
            )
            if not requested
        ]
        if declared_false_unenforced:
            verb = "is" if len(declared_false_unenforced) == 1 else "are"
            reason = (
                "isolate=False was passed for this registration"
                if not isolate
                else "no OS-level enforcement is available on this host/platform"
            )
            self._kb.observability.log(
                logging.WARNING,
                f"Plugin {manifest.name!r} declares "
                f"capabilities.{'/'.join(declared_false_unenforced)}=False, but {verb} not "
                f"enforced for this registration ({reason}) — the plugin can make network "
                "calls / touch the filesystem regardless of this declaration "
                "(ADR-0015, ADR-0051, KI-014).",
                plugin=manifest.name,
                declared_false_unenforced=declared_false_unenforced,
                isolate=isolate,
            )

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

    def _ensure_principal(
        self,
        manifest: PluginManifest,
        effective_capability: str,
        author: str,
        entry_point_name: str,
    ) -> str:
        """Get or create the service principal bound to this plugin, and return its ID."""
        existing = self._kb.get_principal(manifest.name)
        if existing is None:
            principal = self._kb.create_principal(
                manifest.name,
                kind="service",
                auth_method="workload",
                default_capability=effective_capability,
                metadata={
                    _PLUGIN_METADATA_MARKER: True,
                    _ENTRY_POINT_METADATA_KEY: entry_point_name,
                },
                author=author,
            )
            return principal.id

        if existing.kind != "service" or not existing.metadata.get(_PLUGIN_METADATA_MARKER):
            raise PluginError(
                f"Principal {manifest.name!r} already exists and is not a plugin "
                "principal registered by PluginRegistry — refusing to bind to it, "
                "to avoid conflating two unrelated identities under one name"
            )
        existing_entry_point = existing.metadata.get(_ENTRY_POINT_METADATA_KEY)
        # Security review finding, M4 Workstream 7: manifest.name is
        # self-declared by the plugin author, not a unique identifier this
        # project controls. Without this check, a newly registered plugin
        # under a DIFFERENT entry point that happens to declare the same
        # `manifest.name` (and the same computed effective_capability) was
        # silently bound to the existing principal — inheriting its
        # trust_level and attributing its writes under another plugin's
        # identity in the audit trail, with no error at all. A plugin
        # principal created before this fix has no recorded entry point
        # (existing_entry_point is None) and is grandfathered rather than
        # refused, matching this project's existing precedent for a
        # pre-fix shape (e.g. KI-060/KI-078's own migrations).
        if existing_entry_point is not None and existing_entry_point != entry_point_name:
            raise PluginError(
                f"Principal {manifest.name!r} is already bound to entry point "
                f"{existing_entry_point!r}, which differs from {entry_point_name!r} — "
                "refusing to rebind a plugin's identity to a different distribution's "
                "entry point under the same declared name. Choose a distinct "
                "manifest.name for this plugin, or revoke the existing principal first."
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
