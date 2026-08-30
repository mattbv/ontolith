# ADR-0038: Close Two SPEC §17 Authorization Gaps in Principal/Admin Management

**Status:** Accepted

**Date:** 2026-08-30

**Deciders:** Ontolith Core Team

**Related:** ADR-0003 (agent identity, AI-safety guards), ADR-0014 (bearer-token
authentication), ADR-0022 (REST write/review/admin slice — `create_principal`'s
external-gate pattern this ADR extends to the CLI)

## Context

A milestone-boundary security audit (run at M3's close, per project convention
of auditing at milestone boundaries rather than per-PR) found two related SPEC
§17 gaps ("every operation MUST be capability-checked against the resolved
principal"), filed as KI-053 and KI-054:

**KI-053:** `Ontology.require_admin` — the shared gate for token issuance/
revocation, principal listing, schema application, and plugin registration —
checked `default_capability == "admin"` but never `kind`. Every other
capability-tier gate in the codebase already excludes AI-kind principals
regardless of their configured capability
(`_check_direct_write_capability`, `_require_reviewer_principal`,
`resolve_contradiction`'s own inline check) — `admin` sits above all of them
in SPEC §8.3's total order, and it was the one gate that didn't. Concretely: a
misconfigured AI principal with `default_capability="admin"` could call
`issue_token(<some human principal>)`, receive a raw bearer token for that
human, and then authenticate as them over REST or GraphQL — bypassing every
one of the other AI-kind guards and destroying attribution on the resulting
writes.

**KI-054:** `Ontology.create_principal` has no capability check of its own —
a deliberate decision (ADR-0022 §3), matching the CLI's own historically
ungated `principal create` command, on the reasoning that both are
"implicitly trusted" local-process access. REST does not share that trust
boundary (it's network-reachable), so `create_principal_route` compensates by
calling `Ontology.require_admin(principal.id)` explicitly before calling
`create_principal` — the correct pattern. The CLI never adopted that same
pattern: `principal create` remained the one identity-mutating CLI command
with no `--author` option at all, unlike `principal list`, `issue-token`,
`revoke-token`, `list-tokens`, and every `proposal`/`schema` write command.

The audit explicitly noted KI-054 is a *consistency* gap, not a new trust
boundary crossing: CLI access already implies file-level trust equivalent to
raw `sqlite3` manipulation, so an ungated `principal create` was never a
novel escalation path the way KI-053's `require_admin` gap was. It was worth
fixing anyway because SPEC §17's requirement is unconditional, and it was the
only CLI command not matching the established `--author` + gate pattern.

## Decision

**1. `require_admin` now rejects AI-kind principals**, mirroring
`_require_reviewer_principal`'s existing pattern exactly:

```python
def require_admin(self, author: str) -> Principal:
    principal = self.backend.get_principal(author)
    if principal is None:
        raise AuthError(f"Principal not found: {author}")
    if principal.default_capability != "admin":
        raise CapabilityError(f"Principal {author} lacks admin capability")
    if principal.kind == "ai":
        raise CapabilityError(f"Principal {author!r} is an AI principal and cannot hold admin")
    return principal
```

This closes the gap for every one of `require_admin`'s callers at once
(`apply_schema`, `list_principals`, `issue_token`, `revoke_token`,
`list_tokens`, `PluginRegistry.register`) — no call-site changes needed.

**Deliberately not changed:** `Principal`'s own pydantic model, or
`create_principal`, do not reject constructing an AI principal with an
elevated `default_capability` in the first place. Multiple existing
docstrings in `ontology.py` (`_check_direct_write_capability`,
`_require_reviewer_principal`) explicitly anticipate and defend against "an AI
principal misconfigured with elevated capability" as a standing possibility,
not a state that should be impossible to create — the established pattern in
this codebase is defense at every gate, not prevention of the invalid state
existing at all. Adding a create-time rejection would be a broader design
change than this ADR's scope, and isn't needed to close the concrete
escalation path KI-053 describes: `require_admin` (and every capability-tier
gate before it) already independently rejects the AI-kind principal at the
point of use.

**2. The CLI's `principal create` command now requires `--author`, naming an
existing admin principal, with one bootstrap exception.** Mirrors
`create_principal_route`'s already-correct external-gate pattern instead of
changing `Ontology.create_principal`'s own contract:

```python
if author is not None:
    kb.require_admin(author)
elif kb.backend.list_principals():
    # A principal already exists; --author is no longer optional.
    typer.echo("Error: --author is required ...", err=True)
    raise typer.Exit(1)
# else: fresh database, no principals exist yet — allow the bootstrap call.
```

The bootstrap check calls `kb.backend.list_principals()` — the raw
`StorageBackend` port method — rather than `Ontology.list_principals()`,
which itself requires admin and would make bootstrap detection circular (you
can't ask "is there an admin yet" through a method that requires one to
already exist).

**Why keep a bootstrap exception instead of requiring `--author`
unconditionally:** REST's `POST /principals` has the identical bootstrap
problem — it requires an existing admin caller too — and has never solved
it, because the SDK/CLI's own implicitly-trusted local access (ADR-0014's own
reasoning) is where the very first admin principal is meant to come from.
Requiring `--author` unconditionally on the CLI would remove that path
entirely, with no replacement. The exception is narrow: it only fires when
the database has *zero* principals, at which point it's mechanically
inevitable that no `--author` value could ever resolve.

## Rationale

**Why fix `require_admin` at the gate rather than the model:** consistent
with the existing codebase-wide pattern (see Decision §1's "deliberately not
changed" note) — every other AI-kind exclusion in this codebase lives at the
point of use, not at construction time, and there's no reason `admin` should
be the one exception now that it's been brought in line with the others.

**Why mirror REST's pattern for the CLI instead of adding authorization to
`Ontology.create_principal` itself:** `create_principal`'s ungated nature is
an already-recorded, deliberate ADR-0022 decision resting on the "local
process access is implicitly trusted" boundary that also underpins the CLI
and SDK generally. Changing `create_principal`'s own contract would relitigate
that decision for every caller (SDK included), not just close the one place
(the CLI) that hadn't yet adopted the pattern REST already uses correctly.

## Alternatives Considered

**Rejecting AI + elevated-capability at `Principal` construction time,
rejected.** Considered per the audit's own suggested fix, but conflicts with
the established "defense at every gate, not prevention of the state existing"
pattern already documented in multiple docstrings elsewhere in `ontology.py`;
would be a broader design change than closing the concrete escalation path
requires.

**Adding an `author` parameter directly to `Ontology.create_principal`,
rejected.** Would relitigate ADR-0022's already-recorded decision for every
caller (SDK, tests, any future interface), not just the CLI, which was the
only place actually missing the pattern REST already has.

**Requiring `--author` unconditionally on the CLI with no bootstrap
exception, rejected.** Would remove the only path to creating a database's
first principal without dropping into raw SDK/Python access — a strictly
worse ergonomic outcome for zero additional security benefit, since local
file access already implies that same trust level.

## Consequences

**Positive:** Closes a real (if narrow — misconfiguration-gated) privilege-
escalation path in `require_admin`. Brings the CLI's `principal create` in
line with every other identity-mutating CLI command's `--author` convention.
No call-site changes needed anywhere `require_admin` is already used.

**Negative / follow-ups:** `Ontology.create_principal` itself remains
ungated for direct SDK callers — unchanged from ADR-0022, not a regression,
but worth naming since a future audit might re-raise it. The broader question
of whether AI principals should be preventable from ever holding elevated
capability (rather than just blocked at each gate) is intentionally left
open; multiple existing SPEC/CLAUDE.md invariants already assume the "blocked
at the gate" model, so revisiting it is a larger discussion than this ADR's
scope.

## References

- SPEC §17 (security model, capability-checked operations), §8.3 (capability
  ordering)
- ADR-0003 (AI-safety guards), ADR-0014 (bearer-token authentication),
  ADR-0022 (REST write/review/admin slice — the `create_principal_route`
  pattern this ADR extends to the CLI)
- `docs/known-issues.md` KI-053, KI-054
