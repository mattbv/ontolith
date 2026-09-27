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
#
# io_uring_setup/_enter/_register are listed in BOTH _NETWORK_SYSCALLS and
# _FILESYSTEM_SYSCALLS (denied whenever either capability is False, not
# only when both are): io_uring submits socket/connect/send/recv AND
# openat/read/write-shaped operations through kernel worker threads, never
# issuing the syscalls seccomp would otherwise catch individually - the
# standard, well-known seccomp bypass (security review finding). Denying
# io_uring outright for a False-declared capability is conservative (it
# also blocks a plugin's own *legitimate* io_uring use of already-open
# descriptors under filesystem=False, say) but this project has no
# reference plugin using it, and "deny more than strictly necessary" is
# the correct direction for a security boundary to err in.
_NETWORK_SYSCALLS: tuple[str, ...] = (
    "socket",
    "socketpair",
    "socketcall",  # i386's socket-syscall multiplexer
    "connect",
    "accept",
    "accept4",
    "bind",
    "listen",
    "send",
    "recv",  # older, non-x86_64 syscall names for sendto/recvfrom
    "sendto",
    "sendmsg",
    "sendmmsg",
    "recvfrom",
    "recvmsg",
    "recvmmsg",
    "getsockopt",
    "setsockopt",
    "shutdown",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
)

# Filesystem-mutation and new-access syscalls - deliberately excludes
# read/write/close/lseek/fstat, which only operate on already-open file
# descriptors (stdio, the IPC pipe itself) and must keep working under a
# filesystem=False filter for the sandbox's own RPC protocol to function.
# ftruncate IS included despite operating on an already-open descriptor -
# unlike read/write/close it mutates file content/size, which
# filesystem=False means to deny.
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
    "fchmod",
    "fchmodat",
    "chown",
    "fchown",
    "lchown",
    "fchownat",
    "truncate",
    "ftruncate",
    "link",
    "linkat",
    "symlink",
    "symlinkat",
    "mknod",
    "mknodat",
    "execve",
    "execveat",  # the kernel opens the target image internally, bypassing
    # open/openat - round-2 review finding: without this, filesystem=False
    # denies reading a file's contents but not running it as a new program.
    # Bounded even so: an installed filter is inherited across execve
    # (libseccomp sets NO_NEW_PRIVS by default), so the new image runs
    # under the exact same restrictions, not a wider set.
    "fallocate",
    "utime",
    "utimes",
    "utimensat",
    "futimesat",
    "setxattr",
    "lsetxattr",
    "fsetxattr",
    "removexattr",
    "lremovexattr",
    "fremovexattr",
    "name_to_handle_at",
    "open_by_handle_at",  # opens a file without going through open/openat
    "fsopen",
    "fsconfig",
    "fsmount",
    "fspick",
    "move_mount",
    "open_tree",  # the newer (kernel 5.2+) mount API
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
)

# Always denied whenever a filter is installed at all (unconditional on
# either capability's own value - see apply_capability_enforcement's own
# comment) - these have nothing to do with network or filesystem access;
# they read/write another process's memory directly, which would let a
# plugin reach past the process boundary ADR-0051's structural isolation
# otherwise relies on (security review finding).
#
# Attack direction here is child -> parent (the child tracing/reading its
# own ancestor). YAMA's ptrace_scope=1 (a distro default on Ubuntu/Debian,
# not a kernel-wide default - corrected in round 3, an earlier version of
# this comment had the direction backwards) confines a *tracer* to its own
# *descendants*, so it already blocks exactly this direction on hosts that
# have it. This denial is what defends the ptrace_scope=0 hosts instead
# (common on many other distros/containers), where nothing else would stop
# it. Note filesystem=True still leaves an equivalent route open via
# /proc/<ppid>/mem (open/pread, not ptrace/process_vm_* at all) - see
# ADR-0051's own Update section.
_PROCESS_ISOLATION_SYSCALLS: tuple[str, ...] = (
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
)


@dataclass(frozen=True)
class EnforcementResult:
    """Outcome of attempting to install OS-level capability enforcement.

    Attributes:
        applied: True if a real syscall-level filter was installed. Always
            denies at least `_PROCESS_ISOLATION_SYSCALLS`
            (`ptrace`/`process_vm_*`) even when both `capabilities.network`
            and `.filesystem` are declared `True` — those two only add to
            what's denied, they're never the sole reason a filter exists.
        reason: Human-readable explanation, always present when `applied`
            is False (why no enforcement is active on this call), absent
            when True.
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

    # _PROCESS_ISOLATION_SYSCALLS is unconditional - included even when
    # both capabilities are declared True (round-3 review finding: an
    # earlier version returned early in that case, "no restriction
    # needed," which skipped installing a filter at all and left
    # ptrace/process_vm_* undenied for the single most-capable
    # registration a manifest can declare - the network.../filesystem
    # checks below are a ceiling on what a plugin may reach *through*,
    # this is a floor on what it may reach *around*).
    denied: list[str] = list(_PROCESS_ISOLATION_SYSCALLS)
    if not capabilities.network:
        denied.extend(_NETWORK_SYSCALLS)
    if not capabilities.filesystem:
        denied.extend(_FILESYSTEM_SYSCALLS)

    try:
        import errno

        syscall_filter = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
        # Rules are matched per-architecture; seccomp_init only registers
        # the running process's native one. On x86_64 (by far the common
        # case), a 32-bit compat syscall (int 0x80) or an x32 one
        # (syscall number | 0x40000000) would otherwise match no rule at
        # all and fall through to defaction=ALLOW - a known seccomp
        # bypass on this architecture specifically (security review
        # finding). Best-effort: an architecture pyseccomp/this kernel
        # doesn't support is skipped, same posture as an unresolvable
        # syscall name below.
        for compat_arch in (getattr(seccomp.Arch, "X86", None), getattr(seccomp.Arch, "X32", None)):
            if compat_arch is None:
                continue
            try:
                syscall_filter.add_arch(compat_arch)
            except Exception:  # nosec B112 - this architecture isn't available on this
                # kernel/build of libseccomp - skip it, the native architecture's own
                # rules (added below) still apply regardless.
                continue
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
