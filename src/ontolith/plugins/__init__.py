"""Plugin capability isolation (ADR-0015).

Public surface for plugin authors and hosts:
- PluginManifest/PluginCapabilities/PluginKind — what a plugin declares
- ReadOnlyView/WriteView — capability-scoped facades a plugin receives
- PluginRegistry/LoadedPlugin — discovery and least-privilege loading
- Importer/Exporter/Reasoner/Validator/Connector — protocol interfaces a
  plugin implements (SPEC §13.2)
"""

from ontolith.plugins.manifest import PluginCapabilities, PluginKind, PluginManifest
from ontolith.plugins.ports import Connector, Exporter, Importer, Reasoner, Validator
from ontolith.plugins.registry import LoadedPlugin, PluginRegistry
from ontolith.plugins.views import ReadOnlyView, WriteView

__all__ = [
    "PluginKind",
    "PluginCapabilities",
    "PluginManifest",
    "ReadOnlyView",
    "WriteView",
    "PluginRegistry",
    "LoadedPlugin",
    "Importer",
    "Exporter",
    "Reasoner",
    "Validator",
    "Connector",
]
