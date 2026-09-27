"""Backend-agnostic storage-format migration reporting types (SPEC §15, ADR-0052).

Each concrete adapter (`store/sqlite/migrations.py`, `store/duckdb/migrations.py`)
owns its own migration *registry* — the actual `up`/`down` DDL, which is
inherently backend-specific — and returns results shaped as the dataclasses
below so a caller (the CLI, or an SDK user) can report on either backend the
same way. See ADR-0052 for the full design: why format_version exists
separately from the per-namespace `schema_version` SPEC §6.4 already tracks,
why opening an out-of-date file is refused rather than silently upgraded, and
why `reversible` is declared per migration rather than assumed.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MigrationStep:
    """One migration's outcome within a `MigrationReport`.

    `applied` is `False` for every step in a dry-run report (SPEC §15's
    "dry-run mode" requirement) — the step is *pending*, not yet run against
    the file. In a real (non-dry-run) report every step has `applied=True`;
    a migration whose `up()` itself fails raises instead of appearing here
    half-applied, so a report is always either fully successful or absent.
    """

    version: int
    """The format_version this step moves the database *to*."""

    description: str
    """Human-readable summary of what this migration does (and, for an
    irreversible one, why it can't be undone)."""

    reversible: bool
    """Whether this migration's `down()` can restore the prior version.
    See ADR-0052's Decision section for what "reversible" means here —
    the DDL rewrite is invertible; data held only in a column a migration
    drops is still lost on `down()`, same as any additive migration's
    inverse."""

    applied: bool
    """`True` once this step has actually run; `False` in a dry-run report."""


@dataclass(frozen=True)
class MigrationReport:
    """Result of a `migrate_file(..., dry_run=...)` call, either backend."""

    from_version: int
    """The format_version the file was at before this call (as read, or
    inferred for a file that predates format_version tracking — see
    ADR-0052)."""

    to_version: int
    """The backend's current format_version (`CURRENT_FORMAT_VERSION`)."""

    steps: tuple[MigrationStep, ...]
    """Every migration between `from_version` and `to_version`, in the order
    applied (or that would be applied, for a dry run). Empty when the file
    was already current."""

    dry_run: bool
    """Whether this report describes a preview (no writes made) or a real
    migration that already ran."""

    @property
    def up_to_date(self) -> bool:
        """`True` when there was nothing to migrate (`steps` is empty)."""
        return len(self.steps) == 0


__all__ = ["MigrationStep", "MigrationReport"]
