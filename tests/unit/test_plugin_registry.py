"""Unit tests for PluginRegistry (ADR-0015).

Entry-point discovery is exercised by monkeypatching
importlib.metadata.entry_points to return fake EntryPoint objects pointing
at the test-double plugins under tests/fixtures/plugins/.
"""

import tempfile
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core.errors import CapabilityError, PluginError, ValidationError
from ontolith.plugins.registry import PluginRegistry
from ontolith.plugins.views import ReadOnlyView, WriteView

ADMIN = "admin@example.com"
NON_ADMIN = "alice@example.com"


@pytest.fixture
def kb() -> Ontology:
    with tempfile.TemporaryDirectory() as tmpdir:
        kb = Ontology.connect(Path(tmpdir) / "test.db")
        kb.create_principal(ADMIN, kind="human", default_capability="admin")
        kb.create_principal(NON_ADMIN, kind="human", default_capability="write")
        yield kb
        kb.close()


def _entry_point(name: str, module: str, attr: str) -> EntryPoint:
    return EntryPoint(name=name, value=f"{module}:{attr}", group="ontolith.plugins")


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, *eps: EntryPoint) -> None:
    def fake_entry_points(*, group: str) -> list[EntryPoint]:
        assert group == "ontolith.plugins"
        return list(eps)

    monkeypatch.setattr("ontolith.plugins.registry.entry_points", fake_entry_points)


class TestRegisterAuthGate:
    def test_non_admin_author_raises_capability_error(self, kb: Ontology) -> None:
        registry = PluginRegistry(kb)
        with pytest.raises(CapabilityError):
            registry.register("trivial-importer", author=NON_ADMIN)

    def test_unknown_author_raises_auth_error(self, kb: Ontology) -> None:
        from ontolith.core.errors import AuthError

        registry = PluginRegistry(kb)
        with pytest.raises(AuthError):
            registry.register("trivial-importer", author="nobody@example.com")


class TestEntryPointDiscovery:
    def test_unknown_entry_point_raises_plugin_error(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_entry_points(monkeypatch)
        registry = PluginRegistry(kb)
        with pytest.raises(PluginError, match="not found"):
            registry.register("nonexistent-plugin", author=ADMIN)

    def test_missing_manifest_raises_plugin_error(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_entry_points(
            monkeypatch,
            _entry_point("broken", "tests.fixtures.plugins.broken_no_manifest", "BrokenNoManifest"),
        )
        registry = PluginRegistry(kb)
        with pytest.raises(PluginError, match="manifest"):
            registry.register("broken", author=ADMIN)

    def test_non_manifest_attribute_raises_plugin_error(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "broken", "tests.fixtures.plugins.broken_bad_manifest", "BrokenBadManifest"
            ),
        )
        registry = PluginRegistry(kb)
        with pytest.raises(PluginError, match="not a PluginManifest instance"):
            registry.register("broken", author=ADMIN)

    def test_entry_point_load_failure_raises_plugin_error(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_entry_points(
            monkeypatch,
            _entry_point("broken", "tests.fixtures.plugins.nonexistent_module", "Nonexistent"),
        )
        registry = PluginRegistry(kb)
        with pytest.raises(PluginError, match="Failed to load plugin"):
            registry.register("broken", author=ADMIN)

    def test_plugin_construction_failure_raises_plugin_error(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "broken", "tests.fixtures.plugins.broken_raises_on_init", "BrokenRaisesOnInit"
            ),
        )
        registry = PluginRegistry(kb)
        with pytest.raises(PluginError, match="Failed to instantiate plugin"):
            registry.register("broken", author=ADMIN)

    def test_successful_discovery_and_load(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-importer", "tests.fixtures.plugins.trivial_importer", "TrivialImporter"
            ),
        )
        registry = PluginRegistry(kb)
        loaded = registry.register("trivial-importer", author=ADMIN)
        assert loaded.manifest.name == "trivial-importer"
        assert loaded.principal_id == "trivial-importer"

    def test_ambiguous_entry_point_name_raises_plugin_error(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two distributions registering the same entry-point name must not
        silently resolve to whichever happens to be first."""
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-importer", "tests.fixtures.plugins.trivial_importer", "TrivialImporter"
            ),
            _entry_point(
                "trivial-importer", "tests.fixtures.plugins.trivial_validator", "TrivialValidator"
            ),
        )
        registry = PluginRegistry(kb)
        with pytest.raises(PluginError, match="Ambiguous plugin entry point"):
            registry.register("trivial-importer", author=ADMIN)


class TestUnenforcedCapabilityWarning:
    """KI-014: registering a plugin that declares network/filesystem intent
    logs a visible warning, since only capabilities.storage is actually
    enforced - the plugin runs in-process with no isolation."""

    def test_warns_when_network_and_filesystem_requested(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-importer-net-fs",
                "tests.fixtures.plugins.trivial_importer_network_and_filesystem",
                "TrivialImporterNetworkAndFilesystem",
            ),
        )
        registry = PluginRegistry(kb)
        with caplog.at_level("WARNING", logger="ontolith.plugins.registry"):
            registry.register("trivial-importer-net-fs", author=ADMIN)

        [record] = caplog.records
        assert "trivial-importer-net-fs" in record.message
        assert "network/filesystem" in record.message
        assert "but are not enforced" in record.message

    def test_warns_naming_only_filesystem_when_only_filesystem_requested(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Review finding: a helper that always names both capabilities
        regardless of what was actually declared would pass every other
        test in this class - this is the real-world shape too, since
        every shipped reference plugin declares filesystem only."""
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-importer-fs-only",
                "tests.fixtures.plugins.trivial_importer_filesystem_only",
                "TrivialImporterFilesystemOnly",
            ),
        )
        registry = PluginRegistry(kb)
        with caplog.at_level("WARNING", logger="ontolith.plugins.registry"):
            registry.register("trivial-importer-fs-only", author=ADMIN)

        [record] = caplog.records
        assert "capabilities.filesystem=True" in record.message
        assert "capabilities.network=True" not in record.message
        assert "but is not enforced" in record.message

    def test_warns_naming_only_network_when_only_network_requested(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-importer-net-only",
                "tests.fixtures.plugins.trivial_importer_network_only",
                "TrivialImporterNetworkOnly",
            ),
        )
        registry = PluginRegistry(kb)
        with caplog.at_level("WARNING", logger="ontolith.plugins.registry"):
            registry.register("trivial-importer-net-only", author=ADMIN)

        [record] = caplog.records
        assert "capabilities.network=True" in record.message
        assert "capabilities.filesystem=True" not in record.message
        assert "but is not enforced" in record.message

    def test_no_warning_when_neither_requested(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-importer", "tests.fixtures.plugins.trivial_importer", "TrivialImporter"
            ),
        )
        registry = PluginRegistry(kb)
        with caplog.at_level("WARNING", logger="ontolith.plugins.registry"):
            registry.register("trivial-importer", author=ADMIN)

        assert caplog.records == []


class TestCapabilityNegotiation:
    def test_write_capable_kind_resolving_to_read_raises_plugin_error(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reasoner/importer/connector that ends up with only 'read'
        effective capability (e.g. it forgot to declare storage in its
        manifest) cannot do its job - fail fast at registration instead of
        handing back a silently useless ReadOnlyView."""
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-reasoner-default",
                "tests.fixtures.plugins.trivial_reasoner_default_caps",
                "TrivialReasonerDefaultCaps",
            ),
        )
        registry = PluginRegistry(kb)
        with pytest.raises(PluginError, match="needs at least 'propose'"):
            registry.register("trivial-reasoner-default", author=ADMIN)

    def test_manifest_over_grant_is_capped_by_grant(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """trivial-importer's manifest requests storage='write'; granting
        only 'propose' must cap the effective capability at 'propose'."""
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-importer", "tests.fixtures.plugins.trivial_importer", "TrivialImporter"
            ),
        )
        registry = PluginRegistry(kb)
        loaded = registry.register("trivial-importer", author=ADMIN, granted_capability="propose")
        principal = kb.get_principal(loaded.principal_id)
        assert principal is not None
        assert principal.default_capability == "propose"

    def test_read_only_kind_hard_capped_regardless_of_manifest_and_grant(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """trivial-validator's manifest requests storage='write' and it's
        granted 'write' too - a validator (read-only kind) must still be
        capped at 'read'."""
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-validator", "tests.fixtures.plugins.trivial_validator", "TrivialValidator"
            ),
        )
        registry = PluginRegistry(kb)
        loaded = registry.register("trivial-validator", author=ADMIN, granted_capability="write")
        principal = kb.get_principal(loaded.principal_id)
        assert principal is not None
        assert principal.default_capability == "read"
        assert isinstance(loaded.view, ReadOnlyView)
        assert not isinstance(loaded.view, WriteView)

    def test_write_capable_kind_gets_write_view_when_effective_above_read(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-importer", "tests.fixtures.plugins.trivial_importer", "TrivialImporter"
            ),
        )
        registry = PluginRegistry(kb)
        loaded = registry.register("trivial-importer", author=ADMIN, granted_capability="write")
        assert isinstance(loaded.view, WriteView)

    def test_unknown_granted_capability_raises_validation_error(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-importer", "tests.fixtures.plugins.trivial_importer", "TrivialImporter"
            ),
        )
        registry = PluginRegistry(kb)
        with pytest.raises(ValidationError):
            registry.register("trivial-importer", author=ADMIN, granted_capability="bogus")


class TestReRegistration:
    def test_reregistration_with_same_capability_reuses_principal(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-importer", "tests.fixtures.plugins.trivial_importer", "TrivialImporter"
            ),
        )
        registry = PluginRegistry(kb)
        first = registry.register("trivial-importer", author=ADMIN, granted_capability="propose")
        second = registry.register("trivial-importer", author=ADMIN, granted_capability="propose")
        assert first.principal_id == second.principal_id

    def test_reregistration_with_different_capability_raises_plugin_error(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-importer", "tests.fixtures.plugins.trivial_importer", "TrivialImporter"
            ),
        )
        registry = PluginRegistry(kb)
        registry.register("trivial-importer", author=ADMIN, granted_capability="propose")
        with pytest.raises(PluginError, match="already registered"):
            registry.register("trivial-importer", author=ADMIN, granted_capability="write")

    def test_name_squatted_by_non_service_principal_raises_plugin_error(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kb.create_principal("trivial-importer", kind="human", default_capability="write")
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-importer", "tests.fixtures.plugins.trivial_importer", "TrivialImporter"
            ),
        )
        registry = PluginRegistry(kb)
        with pytest.raises(PluginError, match="not a plugin principal"):
            registry.register("trivial-importer", author=ADMIN)

    def test_name_squatted_by_unrelated_service_principal_raises_plugin_error(
        self, kb: Ontology, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-existing service principal that merely happens to share the
        plugin's name and capability level - e.g. an unrelated integration's
        service account - must not be silently bound to, even though its
        kind matches. Only principals PluginRegistry itself created (tagged
        via metadata) are eligible for reuse."""
        kb.create_principal(
            "trivial-importer", kind="service", auth_method="workload", default_capability="propose"
        )
        _patch_entry_points(
            monkeypatch,
            _entry_point(
                "trivial-importer", "tests.fixtures.plugins.trivial_importer", "TrivialImporter"
            ),
        )
        registry = PluginRegistry(kb)
        with pytest.raises(PluginError, match="not a plugin principal"):
            registry.register("trivial-importer", author=ADMIN, granted_capability="propose")
