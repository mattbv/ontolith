# ADR-0029: Validator Invocation — Synchronous at Every Commit Point, with a Separate Completeness Path for `accept_proposal`

**Status**: Accepted
**Date**: 2026-08-14
**Deciders**: Ontolith Core Team
**Related**: SPEC §13.2 (Validator protocol), ADR-0015 (Plugin architecture), ADR-0018 (PolicyStrategy defaults), ADR-0025 (Policy `kb` parameter — `KbView` precedent), ADR-0028 (`required` deferred to the validator layer), KI-010 (`RequiredFieldsValidator`), KI-017, KI-031, KI-040, KI-041, KI-042

---

## Context

The `Validator` protocol (`plugins/ports.py`, SPEC §13.2) has existed since KI-010 with a shipped reference implementation, `RequiredFieldsValidator` — but no code path in `src/ontolith/` ever called `.validate()` on a registered validator (KI-042). Separately, `RequiredFieldsValidator` never read a schema's `PropertyDef.required`/`RelationDef.required`; its rule set was a hand-maintained, schema-independent mapping (KI-041). ADR-0028 deferred core-layer `required` enforcement to this plugin/protocol pair specifically, on the understanding that closing KI-041/KI-042 together would be what makes a schema author's `required=True` mean anything end to end.

Two design questions turned out to be coupled but distinct:

1. **Where in the write path should a `Validator` run at all** (KI-042)?
2. **Does that placement work for `RequiredFieldsValidator`'s specific semantics** — a whole-entity completeness check (KI-041)?

The answer to (2) is no, for a structural reason unrelated to the mechanism chosen for (1): `RequiredFieldsValidator.validate()` resolves the incoming assertion's subject to an `Entity`, then re-queries the *entire current set* of that subject's active predicates — it reports what's missing from a required set, evaluated against the entity's current, cumulative state. But `create_entity` produces an entity with zero assertions, and every property is asserted one call at a time. A required-fields check gated synchronously on *every single assertion write* would reject the first (and every intermediate) legitimate write on any concept with two or more required fields — there is no order of individual `assert_literal`/`propose` calls that isn't "incomplete" until the last one lands. This is the same structural argument ADR-0028 already made for why `required` doesn't belong in core as a per-write gate; it applies identically to a per-write `Validator` invocation of `RequiredFieldsValidator`, or of any validator sharing this whole-entity shape.

## Decision

Two separate, independently configured lists on `Ontology`, each mapped to a distinct invocation point:

**`validators: Sequence[Validator]`** — for validators that check one assertion in isolation. Invoked synchronously and blocking, immediately before the assertion in question is durably applied, at every point an assertion actually commits:
- `assert_literal`, `assert_ref` (direct writes)
- `propose`, `propose_ref` — only their auto-accept branch (a `RequireReview` decision defers the write; nothing has committed yet)
- `_replay_proposal_operations` — shared by `accept_proposal` and `resubmit`'s own auto-accept branch, so a proposal that went through human review (or `resubmit`'s re-evaluation) is not exempt from these validators just because it didn't take the `propose`/`propose_ref` auto-accept path

A failing validator raises `ValidationError`, aborting the write (rolling back the transaction, where one is open). This mirrors where `self.policy.evaluate()` already runs — fail-loud, validate-before-commit, consistent with the project's existing philosophy.

**`completeness_validators: Sequence[Validator]`** — for validators like `RequiredFieldsValidator` that need a batch/end-of-workflow view of an entity's cumulative state. Invoked **only from `accept_proposal`**, once per distinct subject touched by the proposal's replayed operations, after every operation in that proposal has landed in the same transaction. Not invoked from:
- direct writes (`assert_literal`/`assert_ref`) — no multi-operation batch boundary exists to check completeness against
- `propose`/`propose_ref`'s auto-accept branch, or `resubmit`'s auto-accept branch — both bypass human review, the same reason `completeness_validators` isn't run there either; only `accept_proposal`'s reviewed path runs them

Both lists are constructor parameters on `Ontology`/`Ontology.connect` (mirroring `policy`'s existing shape, ADR-0018), not something `PluginRegistry` wires automatically. A validator registered this way receives the live `Ontology` instance itself as `kb`, not a capability-scoped `ReadOnlyView` — see Rationale.

`RequiredFieldsValidator` gains a `from_schema(schema: SchemaIR)` classmethod (KI-041) that derives `required_predicates` by scanning every concept's properties and relations for `required=True`, so a schema's own declaration — not a separately hand-maintained mapping — is what a deployment plugs into `completeness_validators`.

## Rationale

**Why two lists instead of one, or a single list with per-validator "run me here" metadata:** the alternative of one list with a flag (e.g. `Validator.batch: bool`) adds a second configuration surface for what the *call site* already knows unambiguously — whether it's checking one about-to-land assertion or a proposal's fully-landed batch. Two lists mapped to two fixed invocation points is simpler to reason about and impossible to misconfigure into the chicken-and-egg failure this ADR exists to avoid: `RequiredFieldsValidator` structurally cannot go in `validators` (it would break entity construction), so there is no flag to get wrong.

**Why every commit point, not just the four the KI text named:** KI-042's Fix text listed `assert_literal`/`assert_ref`/`propose`/`propose_ref` as candidate call sites, but a proposal that goes to review and is later accepted also commits an assertion — via `_replay_proposal_operations`, called from `accept_proposal` — without ever passing through `propose`'s own auto-accept branch. Running `validators` only at the four originally-named sites would silently exempt every reviewed proposal from per-assertion validation, which is a materially weaker guarantee than "every write is validated" and not something a deployment configuring `validators` would expect.

**Why `RequiredFieldsValidator` gets `accept_proposal` specifically, not "some later trigger":** `accept_proposal` is the one point in this codebase where a proposal's operations — which may span multiple assertions on the same entity — are guaranteed to have all landed together, inside one transaction, before anything else observes the result. It is the natural "this batch is now done" boundary; nothing else in the current write surface has that shape. Requiring completeness only for the *governed, human-reviewed* path is a deliberate, narrower guarantee than "every write eventually gets checked" — see Consequences.

**Why validators run with the live `Ontology` as `kb`, not a `ReadOnlyView`:** `ReadOnlyView`/`WriteView` (`plugins/views.py`) exist to give `PluginRegistry`'s capability negotiation teeth — a plugin discovered via `importlib.metadata` entry points and registered by an admin is untrusted relative to first-party code, so it gets a structurally-limited view. A validator passed directly into `Ontology(validators=..., completeness_validators=...)` at construction time is configured by the same deployment that assembled the `Ontology` in the first place — the identical trust level `PolicyStrategy` already has (ADR-0018: no view-scoping, direct access to a pinned `kb`). Sandboxing it through a view would be theater, not a real boundary, for code the deployment wrote or explicitly chose to load. `PluginRegistry`-loaded validators (a separate, not-yet-wired path — `PluginRegistry` still has no "invoke this on every write" mechanism, and this ADR doesn't add one) continue to receive a real `ReadOnlyView` if and when something invokes them that way.

**Why `Validator.validate()`'s `kb` parameter is now typed `ValidatorKbView`, not `ReadOnlyView`:** `Ontology` needs `Validator`'s type to type-hint its own constructor parameters, but `plugins/ports.py` already imports `ReadOnlyView`/`WriteView` from `plugins/views.py`, which imports `Ontology` — a literal import cycle if `ontology.py` imported `plugins/ports.py` at runtime. `ValidatorKbView` is a minimal structural `Protocol` (`get_entity`/`assertions`/`query`/`as_of`) that both `ReadOnlyView` and `Ontology` already satisfy without any inheritance or runtime import — `ontology.py` only needs `Validator`'s type under `TYPE_CHECKING`, which mypy resolves statically without ever executing the cycle at runtime. This is the exact same shape `govern/policy.py`'s `KbView` already uses, for the identical reason (KI-017, ADR-0025) — not a new pattern, a second application of an established one.

## Consequences

**Positive:**
- Closes KI-042: registered `Validator`s are actually invoked, at every point a write commits.
- Closes KI-041: `RequiredFieldsValidator.from_schema()` makes a schema's `required=True` declaration enforceable end to end, once a deployment wires `completeness_validators=[RequiredFieldsValidator.from_schema(schema)]`.
- No new plugin-loading machinery: both lists are plain constructor parameters, consistent with how `policy`/`embedder` are already configured.

**Negative / follow-ups:**
- **Direct writes and non-reviewed auto-accepts are never checked for completeness.** An entity built entirely through `assert_literal`/`assert_ref`, or through `propose`/`propose_ref` calls that all happen to auto-accept, can remain permanently missing a required field with no `completeness_validators` ever objecting — this is the explicit, accepted scope limitation this ADR settles on (matching the second of two rounds of design confirmation), not an oversight. A deployment that needs completeness enforced on every path, not just reviewed proposals, has no ready-made answer here; it would need its own policy (e.g. a `PolicyStrategy` that forces every write through review) or a future KI.
- **`resubmit`'s auto-accept branch runs `validators` but not `completeness_validators`**, for the same "bypasses human review" reason `propose`/`propose_ref`'s auto-accept does — but this means a `resubmit` that auto-accepts on a *second* attempt (after `request_changes` sent it back once) still gets no completeness check, even though a human reviewer was involved in the original `request_changes` decision. Only a `resubmit` that lands in `RequireReview` and is later accepted via `accept_proposal` gets the completeness check.
- **`PluginRegistry`-loaded `Validator` plugins still have no automatic invocation point.** This ADR wires `Ontology`-constructor-injected validators only; a `Validator` registered via `PluginRegistry.register()` is still never called by anything in `src/ontolith/` unless a deployment separately extracts `LoadedPlugin.instance` and passes it into `Ontology(validators=...)`/`completeness_validators=...)` itself. Unifying these two paths (e.g. having `PluginRegistry.register()` optionally wire a validator into the owning `Ontology`) is out of scope here and not separately tracked as its own KI, since no concrete need for it has surfaced yet.
- `Validator.validate()`'s `kb` parameter type changed from the concrete `ReadOnlyView` to the structural `ValidatorKbView` Protocol. This is a widening, backward-compatible change for any existing implementation typed against `ReadOnlyView` (which still satisfies the new Protocol) — but a third-party `Validator` implementation that hard-codes `kb: ReadOnlyView` in its own method signature will not type-check (under `mypy --strict`) against a call site that passes an `Ontology` instance, since `Ontology` is not nominally a `ReadOnlyView`. It will still work correctly at runtime (duck typing), and updating the signature to `ValidatorKbView` (or leaving it untyped) is the correct fix.

## Alternatives Considered

**One list, no distinction between per-assertion and completeness validators:** Rejected — proven unworkable for `RequiredFieldsValidator` specifically (see Context); any validator sharing its whole-entity shape would hit the same chicken-and-egg failure if forced through the per-assertion path.

**Asynchronous / post-commit validation (validators flag, don't block):** Rejected for the general mechanism — the project's stated philosophy (fail-loud, validate at the edge) and the precedent set by `PolicyStrategy`'s synchronous evaluation both favor blocking. Not reconsidered for `completeness_validators` either, despite the narrower invocation surface: `accept_proposal` already runs inside a transaction, so raising there and rolling back is no more disruptive than the per-assertion case.

**Enforce completeness in core, not via a plugin at all:** Rejected — this is what ADR-0028 already decided against; `required` is validator-backed by SPEC §4, and this ADR does not revisit that.

**Have `PluginRegistry.register()` auto-wire every registered `Validator` into `Ontology.validators`:** Rejected for this ADR's scope — `RequiredFieldsValidator` specifically must NOT go into the per-assertion list, so blind auto-wiring is actively wrong for the one validator this codebase ships. A registry-level "wire this as a completeness validator instead" distinction would need its own manifest-level signal (e.g. a `PluginManifest` field) that doesn't exist yet; left as an open question for whoever unifies the two invocation paths (see Consequences).

## References

- SPEC §13.2 (Validator plugin protocol)
- ADR-0015 (Plugin architecture — capability-scoped views, `PluginRegistry`)
- ADR-0018 (PolicyStrategy defaults — precedent for direct, unsandboxed `kb` access)
- ADR-0025 (Policy `kb` parameter — `KbView` structural-Protocol precedent this ADR reuses for `ValidatorKbView`)
- ADR-0028 (`required` deferred to the validator layer — the structural argument this ADR extends to `Validator` invocation)
- `src/ontolith/plugins/ports.py` (`Validator`, `ValidatorKbView`)
- `src/ontolith/plugins/reference/required_fields_validator.py` (`RequiredFieldsValidator.from_schema`)
- `src/ontolith/ontology.py` (`Ontology.validators`, `Ontology.completeness_validators`, `_run_validators`, `_run_completeness_validators`)
- `docs/known-issues.md` (KI-010, KI-017, KI-031, KI-040, KI-041, KI-042)
