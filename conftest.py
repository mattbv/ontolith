"""Registers Hypothesis profiles (dev/ci/nightly) so pyproject.toml's
[tool.hypothesis.profiles.*] tables are actually read — Hypothesis has no
built-in pyproject.toml support, so the numbers there are inert without
this registration + an explicit profile load.

Select a profile with the HYPOTHESIS_PROFILE env var; defaults to "dev".
Per-test @settings(...) overrides still win for any kwarg they set
explicitly — only unset kwargs fall back to the loaded profile.
"""

import os

from hypothesis import settings

settings.register_profile("dev", max_examples=20, deadline=500)
settings.register_profile("ci", max_examples=100, deadline=1000)
settings.register_profile("nightly", max_examples=1000, deadline=5000)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
