"""Unit tests for PluginManifest/PluginCapabilities (ADR-0015)."""

import pytest
from pydantic import ValidationError

from ontolith.plugins.manifest import PluginCapabilities, PluginManifest


class TestPluginCapabilities:
    def test_defaults_are_least_privilege(self) -> None:
        caps = PluginCapabilities()
        assert caps.storage == "read"
        assert caps.network is False
        assert caps.filesystem is False

    @pytest.mark.parametrize("storage", ["read", "propose", "write"])
    def test_valid_storage_levels_accepted(self, storage: str) -> None:
        caps = PluginCapabilities(storage=storage)  # type: ignore[arg-type]
        assert caps.storage == storage

    @pytest.mark.parametrize("storage", ["review", "admin", "not-a-level"])
    def test_out_of_scope_storage_levels_rejected(self, storage: str) -> None:
        """No manifest can request review/admin — those are human/AI-owner
        capabilities, never a plugin's."""
        with pytest.raises(ValidationError):
            PluginCapabilities(storage=storage)  # type: ignore[arg-type]

    def test_frozen(self) -> None:
        caps = PluginCapabilities()
        with pytest.raises(ValidationError):
            caps.storage = "write"  # type: ignore[misc]


class TestPluginManifest:
    def test_construction_per_kind(self) -> None:
        for kind in ("importer", "exporter", "reasoner", "validator", "embedder", "connector"):
            manifest = PluginManifest(name=f"test-{kind}", version="1.0.0", kind=kind)  # type: ignore[arg-type]
            assert manifest.kind == kind

    def test_default_capabilities_is_read_only(self) -> None:
        manifest = PluginManifest(name="test-plugin", version="1.0.0", kind="exporter")
        assert manifest.capabilities.storage == "read"

    @pytest.mark.parametrize(
        "kind", ["storage_backend", "auth_provider", "policy_strategy", "bogus"]
    )
    def test_out_of_scope_kinds_rejected(self, kind: str) -> None:
        """StorageBackend/AuthProvider/PolicyStrategy plugins are infrastructure
        the framework calls INTO, not principal-scoped actors — deliberately
        excluded from the capability-scoped-view model (ADR-0015)."""
        with pytest.raises(ValidationError):
            PluginManifest(name="test-plugin", version="1.0.0", kind=kind)  # type: ignore[arg-type]

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PluginManifest(name="", version="1.0.0", kind="importer")

    def test_frozen(self) -> None:
        manifest = PluginManifest(name="test-plugin", version="1.0.0", kind="importer")
        with pytest.raises(ValidationError):
            manifest.name = "renamed"  # type: ignore[misc]
