"""OS-level network/filesystem denial for a sandboxed plugin child process
(ADR-0051).

Real, syscall-level enforcement via seccomp is only available on Linux
(`pyseccomp`, an optional dependency gated by `sys_platform == "linux"` in
pyproject.toml — never installed on macOS/Windows). Everywhere else, this
module is a documented, honest no-op: the process/IPC boundary
(`sandbox/runner.py`) is the only isolation a plugin gets, exactly the
"declared but not enforced" gap ADR-0015 already named, now qualified to
"on this platform" rather than "at all" (ADR-0051).

`ERRNO(EPERM)`, not `KILL_PROCESS`, is the deny action: a denied syscall
then surfaces as an ordinary `OSError`/`PermissionError` at the plugin's own
call site, propagated back to the parent through the same exception-wire
path (`sandbox/wire.py`) as any other plugin-raised exception — not a
separate "the child's process died" case the parent has to detect via a
closed pipe.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from ontolith.plugins.manifest import PluginCapabilities

# Syscall names, not numbers - pyseccomp resolves names per the running
# kernel's own architecture, which is more portable across kernel versions
# than hardcoding syscall numbers would be. A name pyseccomp doesn't
# recognize on this kernel/arch is skipped rather than aborting the whole
# filter - best-effort denial of everything resolvable beats no filter at
# all over one unresolvable name.
_NETWORK_SYSCALLS: tuple[str, ...] = (
    "socket",
    "socketpair",
    "connect",
    "accept",
    "accept4",
    "bind",
    "listen",
    "sendto",
    "sendmsg",
    "sendmmsg",
    "recvfrom",
    "recvmsg",
    "recvmmsg",
    "getsockopt",
    "setsockopt",
    "shutdown",
)

# Filesystem-mutation and new-access syscalls - deliberately excludes
# read/write/close/lseek/fstat, which only operate on already-open file
# descriptors (stdio, the IPC pipe itself) and must keep working under a
# filesystem=False filter for the sandbox's own RPC protocol to function.
_FILESYSTEM_SYSCALLS: tuple[str, ...] = (
    "open",
    "openat",
    "openat2",
    "creat",
    "unlink",
    "unlinkat",
    "rename",
    "renameat",
    "renameat2",
    "mkdir",
    "mkdirat",
    "rmdir",
    "chmod",
    "fchmodat",
    "chown",
    "fchownat",
    "truncate",
    "ftruncate",
    "link",
    "linkat",
    "symlink",
    "symlinkat",
)


@dataclass(frozen=True)
class EnforcementResult:
    """Outcome of attempting to install OS-level capability enforcement.

    Attributes:
        applied: True if a real syscall-level filter was installed
            (including the degenerate case of both capabilities declared
            True, so nothing needed denying).
        reason: Human-readable explanation, always present when `applied`
            is False (why no enforcement is active on this call) and
            occasionally present when True (e.g. "no restriction needed").
    """

    applied: bool
    reason: str | None


def enforcement_available() -> bool:
    """True if `apply_capability_enforcement` can install a real, OS-level
    filter on this host/platform right now.

    A query only — never installs anything, so it's safe to call from a
    process that must itself stay unsandboxed (e.g. `PluginRegistry`'s own
    parent process, deciding at registration time whether to warn about
    unenforced capabilities before any child process exists to sandbox).
    """
    if sys.platform != "linux":
        return False
    try:
        import pyseccomp  # noqa: F401
    except Exception:
        return False
    return True


def apply_capability_enforcement(capabilities: PluginCapabilities) -> EnforcementResult:
    """Best-effort install OS-level denial for capabilities.network/
    .filesystem, if both this platform and this host support it.

    Must be called from inside the sandboxed child process, after every
    import the sandbox's own runner infrastructure needs (seccomp filters
    apply going forward only - installing one before Python's own import
    machinery finishes would break the interpreter itself) and immediately
    before invoking the plugin's actual entrypoint method.

    Args:
        capabilities: The registering plugin's declared manifest capabilities.

    Returns:
        An EnforcementResult describing what happened. Never raises - a
        failure to enforce degrades to the documented "not enforced on
        this platform/host" state, the same posture ADR-0015 already
        established, not a hard failure that would make declaring
        network=False/filesystem=False riskier than not declaring it.
    """
    if sys.platform != "linux":
        return EnforcementResult(
            applied=False,
            reason=f"no OS-level syscall-denial primitive available on {sys.platform!r} (ADR-0051)",
        )

    try:
        import pyseccomp as seccomp
    except Exception as exc:
        return EnforcementResult(
            applied=False,
            reason=f"pyseccomp unavailable ({type(exc).__name__}: {exc}) — is libseccomp installed?",
        )

    denied: list[str] = []
    if not capabilities.network:
        denied.extend(_NETWORK_SYSCALLS)
    if not capabilities.filesystem:
        denied.extend(_FILESYSTEM_SYSCALLS)

    if not denied:
        return EnforcementResult(
            applied=True, reason="no restriction needed — both capabilities declared True"
        )

    try:
        import errno

        syscall_filter = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
        for name in denied:
            try:
                syscall_filter.add_rule(seccomp.ERRNO(errno.EPERM), name)
            except Exception:  # nosec B112 - deliberate: syscall name not recognized on
                # this kernel/arch, skip it and keep denying the rest of the list -
                # best-effort denial of everything resolvable beats aborting the
                # whole filter over one unresolvable name.
                continue
        syscall_filter.load()
    except Exception as exc:
        return EnforcementResult(
            applied=False, reason=f"failed to install seccomp filter ({type(exc).__name__}: {exc})"
        )

    return EnforcementResult(applied=True, reason=None)


__all__ = ["EnforcementResult", "apply_capability_enforcement", "enforcement_available"]
