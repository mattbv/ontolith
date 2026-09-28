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
# they read/write another process's memory directly, or duplicate another
# process's file descriptors, either of which would let a plugin reach past
# the process boundary ADR-0051's structural isolation otherwise relies on
# (security review finding).
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
#
# pidfd_getfd (Linux 5.6+) requires PTRACE_MODE_ATTACH permission on the
# target - the same check ptrace(PTRACE_ATTACH, ...) itself requires - but
# is a distinct syscall this floor originally missed (security review
# finding, M4 Workstream 7): on a ptrace_scope=0 host, a child could
# pidfd_open(getppid()) (no permission check of its own - any process can
# open a pidfd for a visible pid) then pidfd_getfd to duplicate one of the
# parent's own open file descriptors (e.g. the SQLite connection's fd, or a
# live REST/MCP client socket) into itself, then use the already-open-
# descriptor syscalls this filter must always allow (read/write, for the
# sandbox's own IPC pipe) on the stolen fd - reaching past the process
# boundary without ever calling ptrace or process_vm_* at all. kcmp and
# process_madvise are denied alongside it as the same *family* of
# cross-process introspection/tampering primitive, not because they share
# an identical permission check: both actually require the weaker
# PTRACE_MODE_READ (not YAMA ptrace_scope-restricted the way ATTACH is, so
# reachable even on a ptrace_scope=1 host), and kcmp specifically is useful
# for locating which fd to steal via pidfd_getfd, not for the theft itself.
# process_mrelease's exact permission model wasn't independently verified
# against kernel source; denied on the same "cross-process, no legitimate
# use inside a plugin call" basis as the rest of this floor.
_PROCESS_ISOLATION_SYSCALLS: tuple[str, ...] = (
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "pidfd_open",
    "pidfd_getfd",
    "pidfd_send_signal",
    "kcmp",
    "process_madvise",
    "process_mrelease",
)


@dataclass(frozen=True)
class EnforcementResult:
    """Outcome of attempting to install OS-level capability enforcement.

    Attributes:
        applied: True if a real syscall-level filter was installed. Always
            denies at least `_PROCESS_ISOLATION_SYSCALLS` (`ptrace`/
            `process_vm_*`/`pidfd_*`/`kcmp`/`process_madvise`/
            `process_mrelease`) even when both `capabilities.network`
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


def _install_seccomp_filter(denied: list[str]) -> EnforcementResult:
    """Shared by `apply_preimport_enforcement` and `apply_capability_
    enforcement` below - both attempt to install one seccomp filter
    denying `denied`, differing only in which syscalls that list contains
    and when each is called relative to `_load_plugin_instance`. Multiple
    filters installed in the same process stack (each successive `load()`
    call only ever adds restriction, never removes one already in force -
    standard Linux seccomp-BPF semantics), so calling this twice in one
    child process, with two different `denied` lists, is safe and is
    exactly how `_child_main` uses it (round-2 review finding, M4
    Workstream 7 - see `apply_preimport_enforcement`'s own docstring).
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

    try:
        import errno

        syscall_filter = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
        try:
            # CTL_TSYNC: apply this filter to every thread in the process,
            # not just the one that calls load() (security review round 3
            # finding). libseccomp's own TSYNC default is off, and a filter
            # with no TSYNC only binds the calling thread - a plugin's own
            # module-level code could otherwise start a thread before this
            # filter installs, and that thread would keep running under
            # only whatever filter existed when IT started, unaffected by
            # anything installed afterward on the main thread. Best-effort,
            # same posture as everything else here: an older libseccomp
            # build without TSYNC support fails this call, not the whole
            # filter installation.
            syscall_filter.set_attr(seccomp.Attr.CTL_TSYNC, 1)
        except Exception:  # nosec B110 - TSYNC unsupported on this libseccomp build;
            # the filter still applies to the calling thread either way.
            pass
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


def apply_preimport_enforcement(capabilities: PluginCapabilities) -> EnforcementResult:
    """Best-effort install of a NARROW seccomp filter before
    `_load_plugin_instance` ever runs — before the plugin's own module is
    imported or constructed at all (round-2 review finding, M4 Workstream
    7, correcting round 1's own attempt at this).

    Denies the always-on process-isolation floor (`_PROCESS_ISOLATION_
    SYSCALLS`) and, if `capabilities.network` is `False`, network syscalls
    too — neither has any legitimate reason to fire during plugin loading
    (`entry_points()`'s metadata scan and `EntryPoint.load()`'s module
    import don't open sockets or ptrace anything), so denying them this
    early costs nothing and closes the "exfiltrate over a socket opened at
    import time, then read/write it after the filter installs" attack for
    network specifically.

    Deliberately does NOT touch filesystem syscalls here, regardless of
    `capabilities.filesystem` — round 1's own version of this fix denied
    filesystem unconditionally at this point too, which broke every
    `filesystem=False` plugin's OWN loading: `entry_points()`/`EntryPoint.
    load()` need real filesystem reads to find and import the plugin's
    module, regardless of what the plugin itself declares about its own
    filesystem usage, and `filesystem=False` is this project's own
    default. Reproduced as a real CI failure (`required-fields-validator`,
    the one shipped reference plugin with default — i.e. `filesystem=
    False` — capabilities, failed to load on Linux+libseccomp with `Plugin
    entry point not found`) before being caught and fixed here. Filesystem
    enforcement stays exactly where it always was: installed in full,
    unconditionally denying `_FILESYSTEM_SYSCALLS` when declared `False`,
    immediately before the plugin's own protocol method is invoked, by
    `apply_capability_enforcement` below — this narrower, earlier filter
    does not change that timing at all. The corresponding gap (a
    `filesystem=False` plugin's own malicious import-time code can still
    read/write files during loading, same as before either fix) remains
    open — see KI-109, extended (round 3) to cover a materially worse
    escalation than "filesystem is merely unenforced during import": that
    same window is also where a plugin's own code can tamper with the
    Python-level state `apply_capability_enforcement` itself depends on
    (reassigning it as a module attribute, or poisoning `sys.modules
    ["pyseccomp"]`) to make the LATER, post-import call falsely report
    success too — see `_child_main`'s own docstring for the concrete
    reproduction and the partial mitigation (capturing a function
    reference before load) this round adds.

    Returns its own `EnforcementResult` (round-3 review finding — this
    used to return `None`): `_child_main` checks it under
    `require_enforcement` before `_load_plugin_instance` ever runs, since
    at that point no plugin code has executed yet and the result is fully
    trustworthy. Not sent to the parent as a separate protocol message
    either way — it's a strict subset of what `apply_capability_
    enforcement`'s own result (for the same call) already reports there.

    Never raises - degrades to `EnforcementResult(applied=False, ...)` the
    same way `apply_capability_enforcement` does.
    """
    denied = list(_PROCESS_ISOLATION_SYSCALLS)
    if not capabilities.network:
        denied.extend(_NETWORK_SYSCALLS)
    return _install_seccomp_filter(denied)


def apply_capability_enforcement(capabilities: PluginCapabilities) -> EnforcementResult:
    """Best-effort install OS-level denial for capabilities.network/
    .filesystem, if both this platform and this host support it.

    Must be called from inside the sandboxed child process, after every
    import the sandbox's own runner infrastructure needs (seccomp filters
    apply going forward only - installing one before Python's own import
    machinery finishes would break the interpreter itself) and immediately
    before invoking the plugin's actual entrypoint method. See
    `apply_preimport_enforcement` above for a narrower filter this same
    process also installs *before* the plugin is loaded at all — that one
    only covers network/the process-isolation floor, deliberately not
    filesystem, for the reason given in its own docstring.

    Args:
        capabilities: The registering plugin's declared manifest capabilities.

    Returns:
        An EnforcementResult describing what happened. Never raises - a
        failure to enforce degrades to the documented "not enforced on
        this platform/host" state, the same posture ADR-0015 already
        established, not a hard failure that would make declaring
        network=False/filesystem=False riskier than not declaring it.
    """
    # _PROCESS_ISOLATION_SYSCALLS is unconditional - included even when
    # both capabilities are declared True (round-3 review finding: an
    # earlier version returned early in that case, "no restriction
    # needed," which skipped installing a filter at all and left
    # ptrace/process_vm_* undenied for the single most-capable
    # registration a manifest can declare - the network.../filesystem
    # checks below are a ceiling on what a plugin may reach *through*,
    # this is a floor on what it may reach *around*). Denying it again
    # here, on top of apply_preimport_enforcement's own copy of the same
    # floor, is redundant but harmless (a second loaded filter denying an
    # already-denied syscall changes nothing) and keeps this function
    # correct standing alone, regardless of whether the caller happened to
    # call apply_preimport_enforcement first.
    denied: list[str] = list(_PROCESS_ISOLATION_SYSCALLS)
    if not capabilities.network:
        denied.extend(_NETWORK_SYSCALLS)
    if not capabilities.filesystem:
        denied.extend(_FILESYSTEM_SYSCALLS)
    return _install_seccomp_filter(denied)


__all__ = [
    "EnforcementResult",
    "apply_capability_enforcement",
    "apply_preimport_enforcement",
    "enforcement_available",
]
