"""Test-double plugins for exercising PluginRegistry discovery/loading.

Not real reference plugins (see KI-010) — just importable classes with a
`.manifest` attribute, monkeypatched into `importlib.metadata.entry_points`
by the tests that use them.
"""
