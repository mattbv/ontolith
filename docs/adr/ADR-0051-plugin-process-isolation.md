# ADR-0051: Plugin Process Isolation

**Status:** Accepted

**Date:** 2026-09-19

**Deciders:** Ontolith Core Team

## Context

ADR-0015 gave plugins a *governance-correctness* boundary: `PluginRegistry.register()` loads a
plugin behind a capability-scoped view (`ReadOnlyView`/`WriteView`), so admin-only methods and the
direct-write bypass are structurally absent from what a plugin can reach. It explicitly did **not**
close `docs/known-issues.md` KI-014's other half: `capabilities.network`/`.filesystem` are declared
in the manifest but never enforced. A plugin still runs fully in-process, sharing the same Python
interpreter, memory space, and OS process as the host application — nothing stops it from
`import requests`-ing out to the network or `open()`-ing an arbitrary file, regardless of what its
manifest declares. ADR-0015 named this explicitly: "Python has no true encapsulation... this design
closes the *structural/accidental* bypass... it does **not** close a plugin author who deliberately
writes code to defeat the convention. That gap closes only with process/wasm isolation."

The Implementation Plan (§8) phases this as "process isolation in v1, wasm/subprocess hardening +
signed registry plugins by 1.0." M4 is that 1.0 milestone. Four real reference plugins
(`CsvImporter`, `JsonExporter`, `RdfExporter`, `RequiredFieldsValidator`) now ship against the
`Importer`/`Exporter`/`Validator` protocols, so — unlike when ADR-0015 rejected building this
"before any real plugin exists to run in it" — there is now real, exercised surface to isolate.

The user chose to build real process isolation now (not an ADR-only scoping pass, not a lighter
in-process-only mitigation), so this ADR records the design decided as part of building it, per the
project's own "record a decision before implementing it" convention — this is a single PR that
ships both.

## Decision

**Each plugin's protocol entrypoint (`import_`/`export`/`derive`/`validate`/`sync`) runs in a
freshly spawned child process, with its `kb` view (and any other live object it's handed)
proxied back to the parent over IPC. Network/filesystem denial is enforced at the OS syscall level
on Linux (via seccomp); on platforms without that primitive, the process/IPC boundary is the only
enforcement, honestly documented as such.**

### Process and IPC model (`src/ontolith/plugins/sandbox/`)

- **One child process per call, not per plugin lifetime.** `multiprocessing.get_context("spawn")`
  is used unconditionally (not `fork`) — `spawn` behaves identically on Linux/macOS/Windows (all
  three are in this project's CI matrix), unlike `fork`, which is POSIX-only and unsafe to mix with
  the threads/locks `Ontology`'s own concurrency story (KI-023/KI-084) already relies on. A fresh
  process per call means no plugin state persists between calls (each entrypoint call reconstructs
  the plugin instance from scratch, same as a fresh `plugin_class()` per invocation) and no partially
  -compromised process lingers to serve a second call — the more conservative, defense-in-depth
  choice over a long-lived worker process, and the reference plugins are already effectively
  stateless per-call (the one plugin with constructor state, `RequiredFieldsValidator`, is only ever
  constructed no-argument by the registry, per its own docstring).
- **The plugin is reconstructed inside the child from the same `entry_point_name` string the
  registry already resolved once in the parent** (for the existing manifest/construction
  validation `PluginRegistry.register()` already performs, unchanged by this ADR) — not by pickling
  the parent's already-constructed instance across the boundary. This means the untrusted plugin's
  own `__init__` runs a second time, once in each process; the parent's copy exists only to validate
  early (preserving `register()`'s existing error-timing contract) and is discarded, never invoked
  again.
- **Exactly one duplex `multiprocessing.Pipe()` per call**, not one channel per proxied object.
  Every message is tagged (`"kb"` for the view argument, `"arg_<position>"` for any other live
  object needing a callback) so a single pipe can carry interleaved requests for more than one
  remote object — in practice at most a `kb` view and one writable target, per the Protocol shapes
  in `plugins/ports.py`. The exchange is fully synchronous: the child blocks on each proxied call
  until it gets a reply, so there is never more than one in-flight request, and no reason for a more
  elaborate multiplexed transport.
- **Object detection is by structural type, not argument position or keyword name.** Any argument
  (positional or keyword) that is a `ReadOnlyView`/`WriteView` instance is always proxied, never
  pickled — this works uniformly across `Importer.import_(source, kb)`, `Exporter.export(kb,
  target)`, `Reasoner.derive(kb)`, `Validator.validate(assertion, kb)`, and `Connector.sync(kb)`
  without hardcoding each protocol's argument order. Any other argument is pickled directly if
  possible (covers the paths/strings/dicts/lists every reference plugin actually passes); if pickling
  fails **and** the value exposes a callable `.write` attribute, it is wrapped in a remote-writable
  proxy instead (covers `JsonExporter`/`RdfExporter`'s documented `io.StringIO`-as-`target` support);
  otherwise `register()`'s caller gets a clear `PluginError` before a child process is even spawned,
  naming the argument type that can't cross the boundary.
- **`RemoteReadOnlyView`/`RemoteWriteView`** (`sandbox/remote_view.py`) mirror `ReadOnlyView`/
  `WriteView`'s exact public method set and subclass relationship (so `isinstance(kb, WriteView)`
  checks inside a plugin's own code still hold), but every method body sends one request over the
  pipe and blocks for the reply instead of calling `Ontology` directly — nothing else. `.query()`/
  `.as_of()` are **not supported in an isolated call** in this pass: both return fluent builder
  objects (`QueryBuilder`/`AsOfView`) that hold a live backend reference and support chained calls,
  which would need their own remote-proxy protocol; no shipped reference plugin calls either method,
  so building that proxy now would be speculative. Calling `kb.query(...)`/`kb.as_of(...)` from
  inside an isolated plugin raises a clear `PluginError` naming this limitation, tracked as this
  ADR's own follow-up (see Consequences), not silently broken.
- **Exceptions cross the boundary pickled directly wherever possible.** `BaseException`'s own
  `__reduce__` includes `__dict__` as reconstruction state, not just `self.args` — so an
  `OntolithError` subclass (`core/errors.py`) round-trips through a plain pickle correctly with no
  special-casing needed: `message` and `detail` both come back exactly as they went in, and so does
  the concrete exception type (verified directly, not assumed — see `sandbox/wire.py`'s tests). A
  non-`OntolithError` exception a plugin's own code raises is pickled the same way (covers ordinary
  `ValueError`/`TypeError`/etc.); if a third-party exception type isn't picklable at all, it
  degrades to a plain `RuntimeError` carrying the original type name and message —
  documented as a known, narrow fidelity loss for that one case, not a silent one (the message
  always survives; only the exact exception *type* doesn't, when it can't).

### Enforcement (`sandbox/enforcement.py`)

- **Linux: real, OS-level denial via seccomp** (`pyseccomp`, an optional dependency gated by the
  `sys_platform == "linux"` environment marker in `pyproject.toml`, so it never installs on
  macOS/Windows). Immediately before the child calls into the plugin's actual entrypoint method —
  after every import the runner infrastructure itself needs, since Python's own import machinery
  does filesystem reads that must not be blocked — a `seccomp.SyscallFilter(defaction=ALLOW)` is
  built and loaded denying (via `ERRNO(EPERM)`, not `KILL_PROCESS`) the network syscall group
  (`socket`, `connect`, `bind`, `listen`, `accept`/`accept4`, `send*`, `recv*`, `getsockopt`,
  `setsockopt`, `shutdown`) when `capabilities.network` is `False`, and the filesystem-mutation
  syscall group (`open`, `openat`, `openat2`, `creat`, `unlink*`, `rename*`, `mkdir*`, `rmdir`,
  `chmod*`, `chown*`, `truncate*`, `link*`, `symlink*`) when `capabilities.filesystem` is `False`.
  `ERRNO(EPERM)` rather than killing the process means a denied syscall surfaces as an ordinary
  Python `OSError`/`PermissionError` at the plugin's own call site — caught and propagated through
  the same exception-wire path as any other plugin-raised exception, not a special "process died"
  case the parent has to detect via a closed pipe. Already-open file descriptors (stdio, the IPC
  pipe itself) are unaffected — the filter blocks *new* filesystem access, not continued use of
  what's already open, so the RPC protocol itself keeps working under a `filesystem=False` filter.
- **macOS/Windows: no OS-level enforcement in this pass.** `sandbox-exec` (macOS) and Windows job
  objects/AppContainers are real primitives that could eventually close this the same way, but each
  is a materially different, platform-specific mechanism from seccomp, unverifiable in this
  project's own development environment, and every one of the three OSes in this project's CI
  matrix (`ubuntu-latest`/`macos-latest`/`windows-latest`) would need its own tested path before
  this ADR could honestly claim it — building two more platform-specific sandboxes speculatively,
  without a way to verify either actually restricts anything, would be worse than shipping the one
  that's real and saying so. On these platforms, a plugin's declared `network=False`/
  `filesystem=False` is still not enforced — the process/IPC boundary (no direct object reachability,
  crash isolation) is the only real improvement there, same class of gap ADR-0015 already
  documented, just now qualified to "on this platform" instead of "at all." `PluginRegistry`'s
  existing registration-time `WARNING` (ADR-0015's 2026-08-30 update) is reworded to say exactly
  this, rather than imply nothing has changed.
- **Enforcement failing to install degrades to the same "not enforced, loudly said so" state, never
  silently.** `pyseccomp`'s import can fail two ways: the package isn't installed at all
  (`ImportError`, expected on macOS/Windows since the dependency is platform-gated) or it's
  installed but `libseccomp.so` isn't present on the host (`RuntimeError`, raised by `pyseccomp`
  itself at import time) — CI's `ubuntu-latest` job installs `libseccomp2` via `apt-get` specifically
  so this path is exercised for real there, not merely imported successfully. Either failure is
  caught once, produces the same "no OS-level enforcement available" result as running on a
  non-Linux platform, and is logged through `kb.observability` (ADR-0044) at `WARNING`, not raised —
  a plugin that declared `network=False` should still run, just without the stronger guarantee, the
  same posture ADR-0015 already established for "declared but not enforced."

### `PluginRegistry` surface

- **`register()` gains `isolate: bool = True`.** Deny-by-default extends here the same way it
  already governs default temporality, MCP's read/propose/flag-only default, and capability floors
  defaulting to `propose`: a plugin registered with no explicit opt-out runs isolated. `isolate=False`
  is the documented escape hatch for a plugin whose `source`/`target` genuinely can't cross a process
  boundary at all (an unpicklable, non-`.write`-shaped live object) or for a fully trusted first-party
  plugin where the overhead of a subprocess per call isn't worth paying — it restores exactly today's
  pre-ADR-0051 behavior, unchanged.
- **`LoadedPlugin.instance` becomes an `IsolatedPluginProxy`** when `isolate=True` — a thin object
  exposing only the one protocol method `manifest.kind` implies (`import_`/`export`/`derive`/
  `validate`/`sync`), matching `plugins/ports.py`'s Protocols exactly. Calling it runs the full
  spawn-proxy-collect-result cycle above and returns/raises exactly what the real, unsandboxed call
  would have. `isinstance(loaded.instance, CsvImporter)`-style checks (a few existed in tests before
  this ADR) no longer hold for an isolated registration — expected, since `loaded.instance` is no
  longer the real object at all; `IsolatedPluginProxy.plugin_class` exposes the class being wrapped
  for exactly this kind of identity check.
- **Everything before that point in `register()` — entry-point resolution, manifest validation,
  capability negotiation, principal creation, the existing unenforced-capability warning, admin-event
  recording — is unchanged.** This ADR only replaces what `LoadedPlugin.instance` *is* and how calling
  its one method behaves; it does not touch how a plugin gets discovered, validated, or granted a
  capability.

## Rationale

**Why a fresh subprocess per call instead of a long-lived worker:** the reference plugins carry no
meaningful state between calls (each entrypoint call is a complete, self-contained unit of work —
import this file, export this snapshot, validate this assertion), so there's no efficiency loss
worth trading away the security benefit of never letting a second call reuse a process a first call
already ran untrusted code in.

**Why type-based object detection instead of one proxy-construction path per plugin kind:** the five
Protocols in `plugins/ports.py` don't agree on where `kb` sits in the argument list (`Exporter`
takes it first, `Importer` takes it second) — hardcoding a position per kind would work today but
silently break the moment a future Protocol's `kb` isn't in the position assumed, with no type
error to catch it (both positions type as `object` from the wire's perspective). Detecting `kb` by
`isinstance(value, ReadOnlyView)` instead makes the marshalling code Protocol-shape-agnostic: it
would keep working correctly even for a new plugin kind this ADR didn't anticipate, as long as that
kind still hands a `ReadOnlyView`/`WriteView` to the plugin somewhere in its call.

**Why seccomp specifically, and why only on Linux:** seccomp-bpf is a mature, well-audited Linux
kernel primitive purpose-built for exactly this ("restrict which syscalls a process may make"),
usable by an unprivileged process (`PR_SET_NO_NEW_PRIVS`, which `pyseccomp`/libseccomp already
handle internally), and testable in this project's own `ubuntu-latest` CI job. macOS's nearest
equivalent, `sandbox-exec`, is an Apple-private, undocumented, and repeatedly-flagged-for-eventual-
removal mechanism with no first-party Python binding this project could adopt with the same
confidence; Windows has no directly comparable per-syscall primitive at all (job objects and
AppContainers restrict different things — processes, tokens, resource limits — not arbitrary syscall
denial). Shipping a real mechanism on the one platform where one exists, rather than a best-effort
approximation on all three that this project has no way to verify actually restricts anything, is
the more honest choice — matching this ADR's own "say so, don't imply a guarantee that isn't there"
stance, the same one ADR-0015 already took for the gap this ADR partially closes.

**Why `ERRNO(EPERM)` instead of `KILL_PROCESS` as the seccomp deny action:** a killed process turns
every denied syscall into an abrupt pipe closure the parent has to detect (`EOFError` on `recv()`)
and translate into some generic "the plugin's process died" error, losing the specific syscall/
capability that triggered it. `ERRNO(EPERM)` instead surfaces as an ordinary Python exception
(`PermissionError`/`OSError`) at the exact call the plugin's own code made, which the sandbox's
already-built exception-wire path (needed regardless, for a plugin's own `ValidationError`/
`ValueError`/etc.) propagates back to the parent with no separate handling required — one mechanism
covers both "the plugin raised on purpose" and "the sandbox denied a syscall," rather than two.

## Consequences

**Positive:**
- ✅ Closes the "Python has no true encapsulation" gap ADR-0015 named as unclosed: a plugin's own
  code, isolated in a separate OS process with only a picklable-message IPC channel to the parent,
  cannot reach `view._kb` or any other live object graph at all — there is no longer an object to
  reach past, on any platform, regardless of seccomp availability. This is real, structural value
  independent of the network/filesystem enforcement question.
- ✅ On Linux, `capabilities.network=False`/`capabilities.filesystem=False` are now genuinely
  enforced at the OS syscall level, closing SPEC §17's "MUST deny undeclared access" for the
  platform this project can actually verify it on.
- ✅ A plugin crash (segfault, unhandled fatal error, resource exhaustion) can no longer take down
  the host process — it's a different OS process now.

**Negative:**
- ⚠️ **macOS/Windows get no OS-level network/filesystem enforcement in this pass** — the same
  "declared but not enforced" gap ADR-0015 left open, now qualified by platform rather than closed
  everywhere. Tracked as this ADR's own follow-up (see below), not re-opening KI-014 from scratch —
  the Linux half is genuinely resolved.
- ⚠️ **`.query()`/`.as_of()` are unavailable from inside an isolated plugin call** — a real, if
  currently unused, capability regression for a hypothetical future plugin wanting the fluent query
  builder rather than `assertions()`'s flat filter. Filed as a follow-up (see below).
- ⚠️ **A subprocess-per-call has real, if modest, latency and resource cost** (process spawn,
  pickling overhead) compared to a direct in-process call — not measured against this project's SPEC
  §9 performance budgets, since none of those budgets name plugin invocation; `isolate=False` is the
  documented opt-out for a caller who has already decided this overhead isn't worth paying for a
  specific, trusted registration.
- ⚠️ **A plugin's constructor still runs once, unsandboxed, in the parent process** (unchanged from
  ADR-0015/`register()`'s existing behavior) purely to validate the manifest and catch a broken
  `__init__` early, before the security-relevant boundary this ADR adds even applies — an operator
  registering a plugin at all is already an explicit trust decision (`register()` requires `admin`
  capability, unchanged), so this is the same trust boundary ADR-0015 already relied on, not a new
  one this ADR introduces.
- ⚠️ **A third-party exception type that isn't picklable loses its exact type crossing the
  boundary** (degrades to a plain `RuntimeError` carrying the original name and message) — documented
  above, a narrow fidelity loss versus `isolate=False`'s exact-type-preserving behavior.

## Alternatives Considered

**A long-lived worker process per registered plugin, reused across calls:** rejected — no shipped
plugin needs cross-call state, and a persistent worker means a single compromised or buggy call
taints every subsequent call to the same plugin for the lifetime of the registration, not just the
one call that went wrong.

**WASM-based plugin execution** (compiling plugins to WASM, run under a WASM runtime like
`wasmtime`): rejected for this pass, matching the Implementation Plan's own phasing ("process
isolation... wasm/subprocess hardening... by 1.0" as two distinct, sequenced items, not one). WASM
would require every plugin author to compile to WASM instead of writing ordinary Python against
`plugins/ports.py`'s Protocols — a fundamentally different plugin-authoring model than the one all
four shipped reference plugins and this project's entry-point-based discovery already assume.
Building it now would mean redesigning plugin authorship from scratch, not hardening the existing
one; tracked as a distinct, later follow-up per the Implementation Plan's own sequencing, not
folded into "process isolation."

**A capability-restricted `StorageBackend`/socket-module monkeypatch instead of OS-level
enforcement:** rejected for the same reason ADR-0015 rejected a `StorageBackend` wrapper for the
storage half — an in-process Python-level restriction (replacing `socket.socket`, `builtins.open`,
etc. inside the child) can be trivially defeated by a deliberately malicious plugin (`ctypes`,
re-importing the original via `importlib.reload` on a saved reference, a compiled extension module
that doesn't go through Python's `socket`/`open` at all) — it would raise the bar for a careless
plugin without closing the gap ADR-0015 already named as the more consequential one: a plugin author
who deliberately writes code to defeat the convention. Only a kernel-enforced boundary (seccomp) or
a genuine process/VM boundary actually closes that.

**Signed registry plugins:** out of scope for this ADR, per the Implementation Plan's own phasing
("wasm/subprocess hardening **+ signed registry plugins**" as one bundled 1.0 item, both still
follow-ups here) — this ADR's isolation model is orthogonal to whether a plugin's source is verified
before it's ever loaded; signing addresses a different threat (a tampered or spoofed plugin
distribution), not what a legitimately-installed plugin's own code can do once it runs.

## Follow-ups filed

- **macOS/Windows OS-level network/filesystem enforcement** — no KI filed as a *new* gap; this
  remains KI-014's own still-open half, now scoped precisely to non-Linux platforms rather than
  every platform.
- **Remote `QueryBuilder`/`AsOfView` proxy for isolated plugins** — filed as **KI-101**: an isolated
  plugin cannot call `kb.query(...)`/`kb.as_of(...)` today; no shipped reference plugin needs it yet,
  but a future `Reasoner`/`Connector` plugin doing anything beyond `assertions()`'s flat filter would.

## References

- `docs/known-issues.md` KI-014 (network/filesystem half; Linux now resolved, macOS/Windows remain
  open per this ADR's own scoping) and **KI-101** (new, the `.query()`/`.as_of()` follow-up above)
- ADR-0015 (Plugin Capability Isolation — the storage-capability half this ADR doesn't change, and
  the "Python has no true encapsulation" gap this ADR closes structurally)
- ADR-0044 (Observability — `kb.observability` is how this ADR's enforcement-degradation warning is
  emitted, not a fixed module logger)
- SPEC §13 (Plugin protocols and discovery), §17 (Security model: "runtime SHOULD isolate execution
  and MUST deny undeclared access (storage, network, filesystem)" — storage already enforced via
  ADR-0015, network/filesystem now enforced on Linux via this ADR)
- `docs/Ontolith_Implementation_Plan.md` §8 (Security & supply chain — the "process isolation...
  wasm/subprocess hardening + signed registry plugins by 1.0" phasing this ADR implements the first
  half of)
