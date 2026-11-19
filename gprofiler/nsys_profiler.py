#
# Copyright (C) 2026 Intel Corporation / Pinterest
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""NVIDIA Nsight Systems (nsys) GPU capture helpers for adhoc flamegraphs.

Host-detect only — nsys is NOT bundled with the agent (same packaging model as
PerfSpect). A live ``nsys launch`` is preferred over a host install: start,
stop, and stats run in that process's mount and pid namespace, with its
binary and TMPDIR. See docs/NSYS_GPU_PROFILING.md.
"""

from __future__ import annotations

import csv
import errno
import glob
import io
import logging
import os
import shlex
import shutil
import subprocess  # nosec B404
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence

logger = logging.getLogger(__name__)

NSYS_DATA_DIRECTORY = "/tmp/gprofiler_nsys"
# Fallback when the Performance Studio request does not name a session.
# The client creates sessions with `nsys launch`; the agent only attaches.
NSYS_LAUNCH_SESSION = "gprofiler-cuda-burn"
# How long a successful `nsys sessions list` may be reused across heartbeats.
_NSYS_SESSIONS_CACHE_SEC = 30.0
_sessions_cache_miss = object()
_sessions_cache: tuple = (0.0, _sessions_cache_miss)
_sessions_cache_lock = threading.Lock()
DEFAULT_NSYS_CANDIDATES = (
    "/usr/local/bin/nsys",
    "/opt/nvidia/nsight-systems/2025.6.1/bin/nsys",
    "/opt/nvidia/nsight-systems/2025.5.1/bin/nsys",
    "/opt/nvidia/nsight-systems/2024.6.2/bin/nsys",
)
# Host nsys sessions are per mount-namespace and per uid. The session dir is
# created by `nsys launch`; its owner is the uid start/stop must assume.
_HOST_NSYS_SESSION_DIR = "/tmp/nvidia/nsight_systems"
_host_session_owner_cache: Optional[tuple] = None
_launch_cache_miss = object()
_launch_cache: tuple = (0.0, _launch_cache_miss)
_launch_cache_lock = threading.Lock()


class _NsysLaunchTarget(NamedTuple):
    """A running ``nsys launch`` the agent can attach to.

    ``exe`` is the binary path inside that process's mount namespace.
    ``foreign_mount`` means the report files are not on the agent's filesystem.
    ``foreign_ipc`` means the session daemon's shared memory is not in the agent's IPC namespace.
    ``foreign_net`` means the session agent's TCP listener is not on the agent's localhost.
    """

    pid: int
    exe: str
    uid: int
    gid: int
    tmpdir: Optional[str]
    foreign_mount: bool
    foreign_pid: bool
    foreign_ipc: bool
    foreign_net: bool = False


def _in_separate_mount_namespace() -> bool:
    try:
        return os.readlink("/proc/self/ns/mnt") != os.readlink("/proc/1/ns/mnt")
    except OSError:
        return False


def _host_session_owner() -> Optional[tuple]:
    """Uid/gid of the host nsys session dir. Successful lookups are cached."""
    global _host_session_owner_cache
    if _host_session_owner_cache is not None:
        return _host_session_owner_cache
    try:
        proc = subprocess.run(  # nosec B603
            ["nsenter", "--mount=/proc/1/ns/mnt", "--", "stat", "-c", "%u %g", _HOST_NSYS_SESSION_DIR],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    parts = (proc.stdout or b"").split()
    if proc.returncode != 0 or len(parts) != 2:
        return None
    try:
        _host_session_owner_cache = (int(parts[0]), int(parts[1]))
    except ValueError:
        return None
    return _host_session_owner_cache


def _proc_cmdline(pid: int) -> List[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _is_nsys_launch_argv(argv: Sequence[str]) -> bool:
    if len(argv) < 2 or Path(argv[0]).name != "nsys":
        return False
    return "launch" in argv[1:]


def _proc_cred(pid: int) -> Optional[tuple]:
    """Real uid/gid of ``pid``."""
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    uid = gid = None
    for line in text.splitlines():
        if line.startswith("Uid:"):
            fields = line.split()
            if len(fields) >= 2:
                uid = int(fields[1])
        elif line.startswith("Gid:"):
            fields = line.split()
            if len(fields) >= 2:
                gid = int(fields[1])
    if uid is None or gid is None:
        return None
    return uid, gid


def _proc_tmpdir(pid: int) -> Optional[str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            logger.warning("cannot read /proc/%s/environ (%s); nsys session temp dir may be wrong", pid, exc)
        return None
    for item in raw.split(b"\0"):
        if item.startswith(b"TMPDIR="):
            value = item.split(b"=", 1)[1].decode("utf-8", "replace")
            return value or None
    return None


def _proc_start_ticks(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    rest = stat.rsplit(")", 1)[-1].split()
    # Field 22 (starttime) is the 20th field after comm.
    if len(rest) < 20:
        return 0
    try:
        return int(rest[19])
    except ValueError:
        return 0


def _ns_differs(pid: int, kind: str) -> bool:
    try:
        return os.readlink(f"/proc/{pid}/ns/{kind}") != os.readlink(f"/proc/self/ns/{kind}")
    except OSError:
        return False


def _launch_target_from_pid(pid: int) -> Optional[_NsysLaunchTarget]:
    argv = _proc_cmdline(pid)
    if not _is_nsys_launch_argv(argv):
        return None
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None
    if exe.endswith(" (deleted)"):
        exe = exe[: -len(" (deleted)")]
    if Path(exe).name != "nsys":
        return None
    cred = _proc_cred(pid)
    if cred is None:
        return None
    uid, gid = cred
    return _NsysLaunchTarget(
        pid=pid,
        exe=exe,
        uid=uid,
        gid=gid,
        tmpdir=_proc_tmpdir(pid),
        foreign_mount=_ns_differs(pid, "mnt"),
        foreign_pid=_ns_differs(pid, "pid"),
        foreign_ipc=_ns_differs(pid, "ipc"),
        foreign_net=_ns_differs(pid, "net"),
    )


def _scan_nsys_launch() -> Optional[_NsysLaunchTarget]:
    """Newest ``nsys launch`` visible through the host pid namespace."""
    best: Optional[_NsysLaunchTarget] = None
    best_started = -1
    found = 0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    self_pid = os.getpid()
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == self_pid:
            continue
        target = _launch_target_from_pid(pid)
        if target is None:
            continue
        found += 1
        started = _proc_start_ticks(pid)
        if best is None or started >= best_started:
            best = target
            best_started = started
    if best is not None:
        logger.info(
            "nsys launch pid %s exe %s uid %s tmpdir %s%s",
            best.pid,
            best.exe,
            best.uid,
            best.tmpdir or "/tmp",
            f" ({found} launch processes; using the newest)" if found > 1 else "",
        )
    return best


def _launch_target_alive(target: _NsysLaunchTarget) -> bool:
    current = _launch_target_from_pid(target.pid)
    return current is not None and current.exe == target.exe


def reset_nsys_launch_target_cache() -> None:
    """Drop the cached ``nsys launch`` lookup. Tests use this."""
    global _launch_cache
    with _launch_cache_lock:
        _launch_cache = (0.0, _launch_cache_miss)


def _nsys_launch_target() -> Optional[_NsysLaunchTarget]:
    """``nsys launch`` to attach to. Cached briefly, same window as the session list."""
    global _launch_cache
    now = time.monotonic()
    with _launch_cache_lock:
        cached_at, cached = _launch_cache
        fresh = cached is not _launch_cache_miss and now - cached_at < _NSYS_SESSIONS_CACHE_SEC
    if fresh:
        if cached is None or _launch_target_alive(cached):
            return cached  # type: ignore[return-value]
    found = _scan_nsys_launch()
    with _launch_cache_lock:
        _launch_cache = (time.monotonic(), found)
    return found


def _launch_fs_root() -> Optional[Path]:
    """``/proc/<pid>/root`` when the launch process is in another mount namespace."""
    target = _nsys_launch_target()
    if target is None or not target.foreign_mount:
        return None
    root = Path(f"/proc/{target.pid}/root")
    try:
        if root.is_dir():
            return root
    except OSError:
        return None
    return None


def _remote_path(local: Path) -> Optional[Path]:
    root = _launch_fs_root()
    if root is None or not local.is_absolute():
        return None
    return Path(str(root) + str(local))


def _mkdir_in_launch_namespace(local_dir: Path) -> None:
    """Create ``local_dir`` inside the launch mount namespace so ``nsys -o`` can write it."""
    remote = _remote_path(local_dir)
    if remote is None:
        return
    try:
        remote.mkdir(parents=True, exist_ok=True)
        os.chmod(remote, 0o1777)
    except OSError:
        logger.debug("could not create %s in the nsys launch namespace", local_dir, exc_info=True)


def _pull_launch_outputs(local_dir: Path) -> None:
    """Copy files nsys wrote in the launch namespace back onto the agent's filesystem."""
    remote = _remote_path(local_dir)
    if remote is None:
        return
    try:
        if not remote.is_dir():
            return
        local_dir.mkdir(parents=True, exist_ok=True)
        for src in remote.iterdir():
            if not src.is_file():
                continue
            shutil.copyfile(src, local_dir / src.name)
    except OSError:
        logger.debug("could not copy nsys outputs from the launch namespace", exc_info=True)


def host_nsys_argv(argv: Sequence[str]) -> List[str]:
    """Leave argv unchanged on the host.

    A live ``nsys launch`` is entered with nsenter (its mount, IPC, network, and
    pid namespaces) and its own binary. Otherwise, in a container, enter the host
    mount namespace as the owner of the host session directory.
    """
    args = [str(part) for part in argv]
    target = _nsys_launch_target()
    if target is not None and (
        target.foreign_mount or target.foreign_ipc or target.foreign_net or target.foreign_pid
    ):
        if args:
            args[0] = target.exe
        if shutil.which("nsenter") is None:
            return args
        prefix = ["nsenter", f"--target={target.pid}"]
        if target.foreign_mount:
            prefix.append("--mount")
        if target.foreign_ipc:
            prefix.append("--ipc")
        # `nsys start` connects to the session agent over TCP on localhost; from
        # the host network namespace that is refused.
        if target.foreign_net:
            prefix.append("--net")
        if target.foreign_pid:
            prefix.append("--pid")
        # --wd with no path uses the launch process cwd, which exists in that
        # mount namespace. --wd=/ is opened in the agent's namespace first, so
        # nsys then dies with current_path: No such file or directory and
        # `sessions list` never prints the launches.
        prefix += ["--wd", "--"]
        if (target.uid != 0 or target.gid != 0) and shutil.which("setpriv") is not None:
            prefix += ["setpriv", f"--reuid={target.uid}", f"--regid={target.gid}", "--clear-groups", "--"]
        return prefix + args
    if target is not None:
        if args:
            args[0] = target.exe
        if (target.uid != 0 or target.gid != 0) and shutil.which("setpriv") is not None:
            return ["setpriv", f"--reuid={target.uid}", f"--regid={target.gid}", "--clear-groups", "--", *args]
        return args
    if not _in_separate_mount_namespace() or shutil.which("nsenter") is None:
        return args
    prefix = ["nsenter", "--mount=/proc/1/ns/mnt", "--"]
    owner = _host_session_owner() if shutil.which("setpriv") else None
    if owner is not None:
        uid, gid = owner
        prefix += ["setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups", "--"]
    return prefix + args


def _nsys_env() -> dict:
    """Environment for nsys commands that act on the client's session.

    nsys looks sessions up under its temp directory (boost temp_directory_path:
    TMPDIR, TMP, TEMP, TEMPDIR, else /tmp). The client's ``nsys launch`` temp dir
    is passed through when we attach to that process. The agent's own values
    (e.g. /tmp/gprofiler_tmp, or an unrelated TEMP) are never forwarded.
    """
    env = _workload_env()
    for var in ("TMPDIR", "TMP", "TEMP", "TEMPDIR"):
        env.pop(var, None)
    target = _nsys_launch_target()
    if target is not None and target.tmpdir:
        env["TMPDIR"] = target.tmpdir
    return env


def _exec_nsys(cmd: Sequence[str], timeout: int) -> Optional[subprocess.CompletedProcess]:
    argv = host_nsys_argv(cmd)
    logger.info("Running %s", " ".join(argv))
    try:
        return subprocess.run(  # nosec B603
            argv,
            env=_nsys_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("%s failed: %s", cmd[1] if len(cmd) > 1 else cmd, exc)
        return None


def _workload_env() -> dict:
    """Environment for the nsys-wrapped workload.

    gProfiler runs as a PyInstaller bundle that prepends its own lib dir
    (/tmp/_MEIxxxx) to LD_LIBRARY_PATH; a spawned workload (e.g. dynamically-linked
    PyTorch) would otherwise pick up the bundle's older libstdc++ and fail to
    import (CXXABI_1.3.8 not found). Prefer PyInstaller's saved *_ORIG value; if it
    is absent, strip any _MEI bundle path from LD_LIBRARY_PATH / LD_PRELOAD so the
    child resolves the system libraries.
    """
    env = os.environ.copy()
    for var in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        orig = env.pop(f"{var}_ORIG", None)
        if orig is not None:
            if orig:
                env[var] = orig
            else:
                env.pop(var, None)
            continue
        current = env.get(var)
        if current:
            cleaned = os.pathsep.join(p for p in current.split(os.pathsep) if p and "/_MEI" not in p)
            if cleaned:
                env[var] = cleaned
            else:
                env.pop(var, None)
    return env


def _local_executable(raw: str) -> Optional[Path]:
    path = Path(raw)
    try:
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve() if path.exists() else path
    except OSError:
        return None
    return None


def find_nsys(explicit_path: Optional[str] = None) -> Optional[Path]:
    """Locate an nsys binary.

    Search order: explicit_path and NSYS_PATH when they exist on this filesystem,
    then a live ``nsys launch`` (its path may exist only in that mount namespace),
    then PATH and the usual host install directories.
    """
    for raw in (explicit_path, os.environ.get("NSYS_PATH")):
        if not raw:
            continue
        found = _local_executable(raw)
        if found is not None:
            return found

    target = _nsys_launch_target()
    if target is not None:
        return Path(target.exe)

    candidates: List[str] = []
    which = shutil.which("nsys")
    if which:
        candidates.append(which)
    candidates.extend(DEFAULT_NSYS_CANDIDATES)
    # Glob any versioned install. Some packages ship only the target-linux-* tree
    # without the bin/ symlink, so check both layouts.
    candidates.extend(sorted(glob.glob("/opt/nvidia/nsight-systems/*/bin/nsys"), reverse=True))
    candidates.extend(sorted(glob.glob("/opt/nvidia/nsight-systems/*/target-linux-*/nsys"), reverse=True))

    seen = set()
    for raw in candidates:
        if not raw or raw in seen:
            continue
        seen.add(raw)
        found = _local_executable(raw)
        if found is not None:
            return found
    return None


def _parse_workload(workload: Optional[str | Sequence[str]]) -> Optional[List[str]]:
    if workload is None or workload == "":
        return None
    if isinstance(workload, (list, tuple)):
        return [str(x) for x in workload]
    return shlex.split(str(workload))


def run_nsys_capture(
    nsys: Path,
    output_prefix: Path,
    duration_sec: int,
    workload_cmd: Optional[Sequence[str]] = None,
    stop_event=None,
    backtraces: bool = False,
) -> Optional[Path]:
    """Run `nsys profile` and return the path to the `.nsys-rep` file, or None.

    backtraces=True records a CPU backtrace per kernel-launching CUDA API call
    (--cudabacktrace needs CPU sampling on, so it swaps -s none for
    process-tree sampling — measurably heavier; keep it opt-in).
    """
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    duration_sec = max(1, int(duration_sec))

    cmd: List[str] = [
        str(nsys),
        "profile",
        "-o",
        str(output_prefix),
        "--force-overwrite=true",
        "-t",
        "cuda,nvtx",
    ]
    if backtraces:
        cmd.extend(["-s", "process-tree", "-b", "dwarf", "--cudabacktrace=kernel"])
    else:
        # CUDA-focused, skip heavy CPU sampling / slow symbol waits where possible.
        cmd.extend(["-s", "none"])

    # Bound every session to the requested window; a long-running workload
    # would otherwise outlive the agent's timeout and yield no report.
    cmd.extend(["--duration", str(duration_sec)])
    workload = list(workload_cmd) if workload_cmd else None
    if workload:
        cmd.extend(workload)
    else:
        # No-op session: sleep so nsys still produces a report.
        # Prefer a real CUDA workload via nsys_workload for useful kern sums.
        cmd.extend(["sleep", str(duration_sec)])

    logger.info("Running nsys capture: %s", " ".join(cmd))
    timeout = duration_sec + 120
    try:
        # nosec B603 — nsys path validated; workload from operator config
        proc = subprocess.run(  # nosec B603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            env=_workload_env(),
        )
        if proc.stdout:
            logger.info("nsys profile output (tail): %s", proc.stdout.decode("utf-8", "replace")[-2000:])
        if proc.returncode != 0:
            # nsys exits with the workload's status: 143 when --duration ends it
            # with SIGTERM, or whatever a failing workload returned. The report
            # is still valid in both cases, so only a missing report is fatal.
            logger.warning("nsys profile exited with code %s; using the report if one was written", proc.returncode)
    except subprocess.TimeoutExpired:
        logger.error("nsys profile timed out after %ss", timeout)
        return None
    except OSError as exc:
        logger.error("Failed to execute nsys: %s", exc)
        return None

    if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
        logger.info("Stop event set during nsys capture; discarding result")
        return None

    rep = Path(str(output_prefix) + ".nsys-rep")
    if not rep.is_file():
        # Some nsys versions may write without forcing the suffix identically.
        matches = list(output_prefix.parent.glob(output_prefix.name + "*.nsys-rep"))
        if matches:
            rep = matches[0]
    if not rep.is_file():
        logger.error("nsys profile completed but .nsys-rep not found at %s", rep)
        return None
    return rep


def _find_nsys_rep(output_prefix: Path) -> Optional[Path]:
    rep = Path(str(output_prefix) + ".nsys-rep")
    if rep.is_file():
        return rep
    matches = sorted(output_prefix.parent.glob(output_prefix.name + "*.nsys-rep"))
    return matches[0] if matches else None


def _nsys_text(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or b"").decode("utf-8", "replace").strip()


def resolve_nsys_session_name(requested: Optional[str]) -> str:
    """Session name for `nsys start` / `nsys stop`. Empty falls back to the default."""
    name = (requested or "").strip()
    if not name:
        return NSYS_LAUNCH_SESSION
    if name.startswith("-") or any(char in name for char in "\n\r\x00"):
        logger.error("Refusing nsys session name %r; using %s", name, NSYS_LAUNCH_SESSION)
        return NSYS_LAUNCH_SESSION
    return name


def parse_nsys_sessions_list(text: str) -> List[Dict[str, str]]:
    """Parse `nsys sessions list` into ``[{name, state}, ...]``.

    The table form is ``ID TIME STATE LAUNCH NAME`` with the session name in the
    last column (it may contain spaces). A ``Name:`` / ``State:`` dump is also
    accepted. Lines that do not name a session are ignored.
    """
    sessions: List[Dict[str, str]] = []
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return sessions

    header = lines[0].split()
    header_upper = [col.upper() for col in header]
    if "NAME" in header_upper:
        name_idx = header_upper.index("NAME")
        state_idx = header_upper.index("STATE") if "STATE" in header_upper else None
        for line in lines[1:]:
            parts = line.split()
            if len(parts) <= name_idx:
                continue
            name = " ".join(parts[name_idx:]).strip()
            state = parts[state_idx] if state_idx is not None and len(parts) > state_idx else ""
            if name:
                sessions.append({"name": name, "state": state})
        return sessions

    current: Dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "name" and value:
            if current.get("name"):
                sessions.append({"name": current["name"], "state": current.get("state", "")})
            current = {"name": value, "state": ""}
        elif key == "state" and current.get("name"):
            current["state"] = value.split()[0] if value else ""
    if current.get("name"):
        sessions.append({"name": current["name"], "state": current.get("state", "")})
    return sessions


def reset_nsys_sessions_cache() -> None:
    """Drop the cached `nsys sessions list`. Tests use this."""
    global _sessions_cache
    with _sessions_cache_lock:
        _sessions_cache = (0.0, _sessions_cache_miss)


def list_nsys_sessions() -> Optional[List[Dict[str, str]]]:
    """Sessions from `nsys sessions list`.

    ``None`` when nsys is not installed or the list command failed — callers
    omit the field so a previous report is kept. A list (possibly empty) means
    nsys ran. Results are cached briefly so heartbeats do not spawn nsys every time.
    """
    global _sessions_cache
    now = time.monotonic()
    with _sessions_cache_lock:
        cached_at, cached = _sessions_cache
        if cached is not _sessions_cache_miss and now - cached_at < _NSYS_SESSIONS_CACHE_SEC:
            return cached  # type: ignore[return-value]

    nsys = find_nsys()
    if nsys is None:
        result: Optional[List[Dict[str, str]]] = None
    else:
        listed = _exec_nsys([str(nsys), "sessions", "list"], timeout=10)
        if listed is None or listed.returncode != 0:
            result = None
        else:
            result = parse_nsys_sessions_list(_nsys_text(listed))

    with _sessions_cache_lock:
        _sessions_cache = (time.monotonic(), result)
    return result


def _status_is_collecting(text: str) -> bool:
    # "collecting data" is the live state. The idle line
    # "use the 'launch' command to start the collection" must not match.
    return "collecting" in text.lower()


class NsysLaunchSession:
    """Collection open on a client-created nsys session.

    The client runs `nsys launch`. Start Profiling runs `nsys start` on the
    session named by the request and holds the collection open for the requested
    duration. Stop Profiling, or the duration elapsing, runs `nsys stop`. The
    agent does not launch the workload.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.collecting = False
        self.output_prefix: Optional[Path] = None
        self.backtraces = False
        self.session_name = NSYS_LAUNCH_SESSION

    def reset(self) -> None:
        with self._lock:
            self.collecting = False
            self.output_prefix = None
            self.backtraces = False
            self.session_name = NSYS_LAUNCH_SESSION

    def _log_sessions(self, nsys: Path) -> None:
        listed = _exec_nsys([str(nsys), "sessions", "list"], timeout=30)
        if listed is None:
            return
        logger.error("nsys sessions list (exit %s):\n%s", listed.returncode, _nsys_text(listed) or "<empty>")

    def start_collection(
        self,
        nsys: Path,
        output_prefix: Path,
        backtraces: bool = False,
        session_name: Optional[str] = None,
    ) -> bool:
        """`nsys start --session=<name>`, then require status to say collecting."""
        name = resolve_nsys_session_name(session_name)
        with self._lock:
            if self.collecting:
                logger.warning("nsys collection is already open; not starting another")
                return False
            self.backtraces = backtraces
            self.session_name = name
            output_prefix.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(output_prefix.parent, 0o1777)  # launch user writes the report via nsenter
            except OSError:
                pass
            # The same path has to exist in the launch mount namespace: nsys -o
            # resolves there, not on the agent's filesystem.
            _mkdir_in_launch_namespace(output_prefix.parent)
            cmd = [
                str(nsys),
                "start",
                f"--session={name}",
                "-o",
                str(output_prefix),
                "--force-overwrite=true",
            ]
            # cudabacktrace is set by the client's `nsys launch`. Sampling has to
            # be on for nsys to keep those stacks. -s none drops them.
            if backtraces:
                cmd.extend(["-s", "process-tree", "-b", "dwarf"])
            else:
                cmd.extend(["-s", "none"])
            proc = _exec_nsys(cmd, timeout=60)
            if proc is None or proc.returncode != 0:
                logger.error(
                    "nsys start --session=%s failed (rc=%s): %s",
                    name,
                    None if proc is None else proc.returncode,
                    "" if proc is None else _nsys_text(proc)[-1000:],
                )
                self._log_sessions(nsys)
                return False
            status_text = ""
            for _ in range(25):
                status = _exec_nsys([str(nsys), "status", f"--session={name}"], timeout=10)
                if status is not None and status.returncode == 0:
                    status_text = _nsys_text(status)
                    if _status_is_collecting(status_text):
                        logger.info(
                            "nsys status --session=%s: %s",
                            name,
                            status_text,
                        )
                        self.collecting = True
                        self.output_prefix = output_prefix
                        return True
                time.sleep(0.2)
            logger.error(
                "nsys start returned but status --session=%s is not collecting (%r)",
                name,
                status_text,
            )
            self._log_sessions(nsys)
            # start already returned 0, so close the collection instead of leaving it open.
            _exec_nsys([str(nsys), "stop", f"--session={name}"], timeout=300)
            return False

    def stop_collection(self, nsys: Path) -> Optional[Path]:
        """`nsys stop` the session this collection opened. The client's workload keeps running."""
        with self._lock:
            if not self.collecting:
                logger.info("nsys stop with no open collection")
                return None
            prefix = self.output_prefix
            name = self.session_name
            proc = _exec_nsys([str(nsys), "stop", f"--session={name}"], timeout=300)
            self.collecting = False
            if proc is None:
                return None
            if proc.returncode != 0:
                logger.error(
                    "nsys stop exited with code %s: %s",
                    proc.returncode,
                    (proc.stdout or b"")[-1000:].decode("utf-8", "replace"),
                )
                return None
            if prefix is None:
                return None
            _pull_launch_outputs(prefix.parent)
            rep = _find_nsys_rep(prefix)
            if rep is None:
                logger.error("nsys stop completed but .nsys-rep not found at %s", prefix)
            return rep


_nsys_launch_session = NsysLaunchSession()


def reset_nsys_launch_session() -> None:
    """Drop session state. Tests use this; the agent process keeps one session."""
    _nsys_launch_session.reset()


def collect_nsys_launch_html(
    *,
    nsys: Path,
    duration_sec: int,
    work_dir: Path,
    stop_event=None,
    timeline: bool = False,
    timeline_stacks: bool = False,
    on_rep: Optional[Callable[[Path], None]] = None,
    generate_html_fn: Optional[Callable[[str], Optional[str]]] = None,
    session_name: Optional[str] = None,
) -> Optional[str]:
    """`nsys start` the named client session, hold it for duration_sec, `nsys stop`, build HTML.

    Stop Profiling sets stop_event and ends the collection early. The configured
    duration is the maximum. The client's `nsys launch` process is not ours and
    is not watched.

    Each capture gets its own directory so `nsys stats` cannot skip a leftover
    CSV from an earlier run.
    """
    session = _nsys_launch_session

    from datetime import datetime

    capture_dir = work_dir / datetime.now().strftime("launch_%Y%m%dT%H%M%S_%f")
    output_prefix = capture_dir / "gprofiler_nsys_capture"
    name = resolve_nsys_session_name(session_name)
    if not session.start_collection(nsys, output_prefix, backtraces=timeline_stacks, session_name=name):
        return None

    duration_sec = max(1, int(duration_sec))
    started = time.monotonic()
    deadline = started + duration_sec
    logger.info(
        "nsys session %s collecting for up to %ss (Stop Profiling ends it early)",
        name,
        duration_sec,
    )
    while time.monotonic() < deadline:
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            logger.info(
                "Stop Profiling after %.1fs; ending nsys session %s (configured %ss)",
                time.monotonic() - started,
                name,
                duration_sec,
            )
            break
        time.sleep(0.2)
    else:
        logger.info("nsys session %s reached the configured duration %ss", name, duration_sec)

    rep = session.stop_collection(nsys)
    if rep is None:
        return None
    if on_rep is not None:
        try:
            on_rep(rep)
        except Exception:
            logger.exception("on_rep callback failed; continuing with HTML generation")
    return _html_from_rep(
        nsys,
        rep,
        capture_dir,
        timeline=timeline,
        timeline_stacks=timeline_stacks,
        generate_html_fn=generate_html_fn,
        with_stacks=timeline_stacks,
    )


def _html_from_rep(
    nsys: Path,
    rep: Path,
    work_dir: Path,
    *,
    timeline: bool,
    timeline_stacks: bool,
    generate_html_fn: Optional[Callable[[str], Optional[str]]],
    with_stacks: bool = False,
) -> Optional[str]:
    if timeline:
        events = nsys_trace_to_timeline_events(nsys, rep, work_dir, with_stacks=timeline_stacks)
        if events:
            html = generate_nsys_timeline_html(events)
            if html:
                logger.info("Generated nsys CPU/GPU timeline HTML (%d bytes)", len(html))
                return html
        logger.warning("nsys timeline requested but no trace events; falling back to GPU flamegraph")

    collapsed = nsys_stats_to_collapsed(nsys, rep, work_dir, with_stacks=with_stacks)
    if not collapsed:
        return None

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    def _gen(collapsed_data: str) -> Optional[str]:
        if generate_html_fn is not None:
            return generate_html_fn(collapsed_data)
        return generate_nsys_flamegraph_html(
            collapsed_data,
            now.isoformat(),
            now.isoformat(),
            generate_html_fn=None,
        )

    html = _gen(collapsed)
    if html:
        logger.info("Generated nsys GPU flamegraph HTML (%d bytes)", len(html))
    else:
        logger.error("Failed to generate nsys flamegraph HTML from collapsed stacks")
    return html


def _csv_to_collapsed(csv_text: str, frame_prefix: str) -> Optional[str]:
    """Convert an nsys stats CSV into collapsed stacks.

    Uses Total Time (ns) (or the first numeric time-like column) as weight.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return None

    # Normalize header keys (strip BOM / whitespace)
    fieldnames = [f.strip().lstrip("\ufeff") for f in reader.fieldnames]
    reader.fieldnames = fieldnames

    name_key = next((f for f in fieldnames if f.lower() in ("name", "kernel name", "kernel")), None)
    time_key = next(
        (
            f
            for f in fieldnames
            if f.lower() in ("total time (ns)", "total time(ns)", "total (ns)", "time (ns)")
        ),
        None,
    )
    if name_key is None:
        # Fall back to last column as name (nsys puts Name last)
        name_key = fieldnames[-1]
    if time_key is None:
        # Prefer any column containing 'total' and 'ns'
        time_key = next(
            (f for f in fieldnames if "total" in f.lower() and "ns" in f.lower()),
            None,
        )
    if time_key is None:
        time_key = next((f for f in fieldnames if "time" in f.lower()), None)

    lines: List[str] = []
    for row in reader:
        # DictReader may still use original keys; build a stripped map.
        cleaned = { (k or "").strip().lstrip("\ufeff"): (v or "").strip() for k, v in row.items() }
        name = cleaned.get(name_key, "").strip().strip('"')
        if not name:
            continue
        raw_time = cleaned.get(time_key or "", "0") or "0"
        try:
            # Total Time may be float ns
            weight = max(1, int(float(raw_time)))
        except ValueError:
            weight = 1
        # Scale down huge ns values to keep flamegraph samples manageable
        # (1 sample ≈ 1µs of GPU time). Keep at least 1.
        samples = max(1, weight // 1000)
        # A qualified kernel name is the call stack when CPU sampling is unavailable.
        # cuda_deep names kernels run_workload::train_step::...::gemm_fma so the
        # flamegraph shows those frames instead of a single leaf.
        head, args = (name.split("(", 1) + [""])[:2]
        parts = [part.strip() for part in head.split("::") if part.strip()]
        if not parts:
            parts = [name]
        if args:
            parts[-1] = parts[-1] + "(" + args
        safe_name = ";".join(part.replace(";", ",") for part in parts)
        lines.append(f"{frame_prefix};{safe_name} {samples}")

    return "\n".join(lines) if lines else None


def _export_stats_csv(nsys: Path, nsys_rep: Path, work_dir: Path, report: str, out_prefix: Path) -> Optional[str]:
    """Run `nsys stats --report=<report> --format=csv` and return the CSV text, or None."""
    # nsys writes no CSV for a report with no rows (e.g. a capture without CUDA
    # activity), so a previous capture's CSV would otherwise be picked up below.
    for stale in work_dir.glob(out_prefix.name + "*.csv"):
        stale.unlink(missing_ok=True)
    cmd = [
        str(nsys),
        "stats",
        f"--report={report}",
        "--format=csv",
        "--force-export=true",
        "-o",
        str(out_prefix),
        str(nsys_rep),
    ]
    proc = _exec_nsys(cmd, timeout=120)
    if proc is None:
        return None
    _pull_launch_outputs(work_dir)
    _pull_launch_outputs(nsys_rep.parent)
    if proc.returncode != 0:
        logger.warning(
            "nsys stats %s failed (rc=%s): %s",
            report,
            proc.returncode,
            (proc.stdout or b"")[-1000:].decode("utf-8", "replace"),
        )

    matches = sorted(work_dir.glob(out_prefix.name + "*.csv"))
    # Also check CWD-relative names nsys sometimes writes
    matches += sorted(Path(".").glob(out_prefix.name + "*.csv"))
    matches += sorted(nsys_rep.parent.glob(out_prefix.name + "*.csv"))
    # Dedup
    seen = set()
    unique = []
    for m in matches:
        rp = str(m.resolve()) if m.exists() else str(m)
        if rp not in seen and m.is_file() and m.stat().st_size > 0:
            seen.add(rp)
            unique.append(m)
    if not unique:
        logger.info("nsys stats %s produced no non-empty CSV", report)
        return None
    return unique[0].read_text(encoding="utf-8", errors="replace")


_SKIP_MODULE_SNIPPETS = (
    "libcudart",
    "libcuda.so",
    "libc.so",
    "ld-linux",
    "libpthread",
    "libdl.so",
    "libnsight",
    "linux-vdso",
    "libstdc++",
    "libgcc_s",
    "libm.so",
)


def _host_frame(frame: str) -> Optional[str]:
    """Keep an application frame. Drop the CUDA runtime and libc frames nsys adds."""
    symbol = frame
    module = ""
    if frame.endswith(")") and " (" in frame:
        symbol, module = frame[:-1].rsplit(" (", 1)
    if any(snippet in module.lower() for snippet in _SKIP_MODULE_SNIPPETS):
        return None
    low = symbol.lower()
    if "cuda" in low or low.startswith("cu"):
        return None
    symbol = symbol.replace(";", ",").strip()
    return symbol or None


def callchains_to_collapsed(gpu_events: List[dict], corr_to_stack: dict, stacks: List[List[str]]) -> Optional[str]:
    """Collapsed stacks whose width is GPU kernel time and whose frames are the CPU call chain.

    stacks are innermost-first, as load_callchains_from_sqlite returns them.
    Collapsed lines are outermost-first, kernel name at the leaf.
    """
    weights: dict = {}
    for ev in gpu_events:
        dur = int(ev.get("dur") or 0)
        if dur <= 0:
            continue
        frames: List[str] = []
        corr = ev.get("corr")
        idx = corr_to_stack.get(corr) if corr is not None else None
        if idx is not None and 0 <= idx < len(stacks):
            for raw in reversed(stacks[idx]):
                kept = _host_frame(raw)
                if kept:
                    frames.append(kept)
        kernel = str(ev.get("name") or "").replace(";", ",").strip()
        if not kernel:
            continue
        frames.append(kernel)
        key = "gpu;nsys;cuda_kernel;" + ";".join(frames)
        weights[key] = weights.get(key, 0) + dur
    if not weights:
        return None
    lines = []
    for key, total_ns in sorted(weights.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"{key} {max(1, total_ns // 1000)}")
    return "\n".join(lines)


def _collapsed_from_backtraces(nsys: Path, nsys_rep: Path, work_dir: Path) -> Optional[str]:
    """Build a deep flamegraph from CUDA launch backtraces, or None if the report has none."""
    sqlite_path = nsys_export_sqlite(nsys, nsys_rep, work_dir)
    if sqlite_path is None:
        return None
    corr_to_stack, stacks = load_callchains_from_sqlite(sqlite_path)
    if not stacks:
        return None
    gpu_csv = _export_stats_csv(nsys, nsys_rep, work_dir, "cuda_gpu_trace", work_dir / "gpu_trace_stacks")
    if not gpu_csv:
        return None
    events = _parse_trace_csv(gpu_csv, "gpu")
    collapsed = callchains_to_collapsed(events, corr_to_stack, stacks)
    if collapsed:
        logger.info(
            "Built collapsed stacks from CUDA backtraces (%d lines)", collapsed.count("\n") + 1
        )
    return collapsed


def nsys_stats_to_collapsed(
    nsys: Path, nsys_rep: Path, work_dir: Path, with_stacks: bool = False
) -> Optional[str]:
    """Export cuda_gpu_kern_sum (fallback cuda_api_sum) and convert to collapsed stacks.

    With with_stacks=True and a capture that used --cudabacktrace, the lines include
    the CPU call chain above each kernel so the flamegraph is more than one frame deep.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    if with_stacks:
        deep = _collapsed_from_backtraces(nsys, nsys_rep, work_dir)
        if deep:
            return deep

    def _export(report: str, out_prefix: Path) -> Optional[str]:
        return _export_stats_csv(nsys, nsys_rep, work_dir, report, out_prefix)

    kern_csv = _export("cuda_gpu_kern_sum", work_dir / "kern")
    if kern_csv:
        collapsed = _csv_to_collapsed(kern_csv, "gpu;nsys;cuda_kernel")
        if collapsed:
            logger.info("Built collapsed stacks from cuda_gpu_kern_sum (%d lines)", collapsed.count("\n") + 1)
            return collapsed

    api_csv = _export("cuda_api_sum", work_dir / "api")
    if api_csv:
        collapsed = _csv_to_collapsed(api_csv, "gpu;nsys;cuda_api")
        if collapsed:
            logger.info("Built collapsed stacks from cuda_api_sum fallback (%d lines)", collapsed.count("\n") + 1)
            return collapsed

    logger.error("No usable nsys stats CSV (cuda_gpu_kern_sum / cuda_api_sum)")
    return None


# --- CPU/GPU timeline (cuda_gpu_trace + cuda_api_trace) ---------------------

# Self-contained timeline HTML gets large fast; keep the longest events if the
# capture has more than this many.
MAX_TIMELINE_EVENTS = 20000


def _find_column(fieldnames: List[str], *needles: str) -> Optional[str]:
    """First column whose lowercase name contains any needle (checked in order)."""
    lowered = [(f, f.lower()) for f in fieldnames]
    for needle in needles:
        for original, low in lowered:
            if needle in low:
                return original
    return None


def _parse_trace_csv(csv_text: str, kind: str) -> List[dict]:
    """Parse an nsys cuda_gpu_trace / cuda_api_trace CSV into timeline events.

    kind is "gpu" or "api". Returns events with keys:
    start (ns), dur (ns), name, corr (int|None), lane (str).
    GPU lanes group by device+stream; API lanes group by pid/tid.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return []
    fieldnames = [f.strip().lstrip("\ufeff") for f in reader.fieldnames]
    reader.fieldnames = fieldnames

    start_key = _find_column(fieldnames, "start (ns)", "start")
    dur_key = _find_column(fieldnames, "duration (ns)", "duration")
    name_key = next((f for f in fieldnames if f.lower() in ("name", "kernel name", "kernel")), None)
    corr_key = _find_column(fieldnames, "corrid")
    if start_key is None or dur_key is None or name_key is None:
        logger.warning("nsys %s trace CSV missing start/duration/name columns: %s", kind, fieldnames)
        return []

    device_key = stream_key = pid_key = tid_key = None
    if kind == "gpu":
        device_key = _find_column(fieldnames, "device")
        stream_key = _find_column(fieldnames, "strm", "stream")
    else:
        pid_key = _find_column(fieldnames, "pid")
        tid_key = _find_column(fieldnames, "tid")

    events: List[dict] = []
    for row in reader:
        cleaned = {(k or "").strip().lstrip("\ufeff"): (v or "").strip() for k, v in row.items()}
        name = cleaned.get(name_key, "").strip().strip('"')
        if not name:
            continue
        try:
            start = int(float(cleaned.get(start_key, "")))
            dur = max(0, int(float(cleaned.get(dur_key, ""))))
        except ValueError:
            continue
        corr: Optional[int] = None
        if corr_key:
            try:
                corr = int(float(cleaned.get(corr_key, "")))
            except ValueError:
                corr = None
        if kind == "gpu":
            device = cleaned.get(device_key, "") if device_key else ""
            stream = cleaned.get(stream_key, "") if stream_key else ""
            lane = f"GPU {device or '?'} stream {stream or '?'}"
        else:
            pid = cleaned.get(pid_key, "") if pid_key else ""
            tid = cleaned.get(tid_key, "") if tid_key else ""
            lane = f"CPU pid {pid or '?'} tid {tid or '?'}"
        events.append({"start": start, "dur": dur, "name": name, "corr": corr, "lane": lane})
    return events


def nsys_export_sqlite(nsys: Path, nsys_rep: Path, work_dir: Path) -> Optional[Path]:
    """Run `nsys export --type sqlite` and return the .sqlite path, or None."""
    out = work_dir / (nsys_rep.stem + ".sqlite")
    cmd = [
        str(nsys),
        "export",
        "--type",
        "sqlite",
        "--force-overwrite",
        "true",
        "--output",
        str(out),
        str(nsys_rep),
    ]
    proc = _exec_nsys(cmd, timeout=300)
    _pull_launch_outputs(work_dir)
    if proc is None or proc.returncode != 0:
        if proc is not None:
            logger.warning(
                "nsys export sqlite failed (rc=%s): %s",
                proc.returncode,
                (proc.stdout or b"")[-1000:].decode("utf-8", "replace"),
            )
        return None
    if not out.is_file() or out.stat().st_size == 0:
        logger.warning("nsys export sqlite produced no file at %s", out)
        return None
    return out


def load_callchains_from_sqlite(sqlite_path: Path) -> tuple:
    """Read CUDA API backtraces from an nsys SQLite export.

    Returns (corr_to_stack, stacks): correlationId → index into stacks, where
    each stack is a list of "symbol (module)" frames, innermost first. Stacks
    are deduped — CUDA_CALLCHAINS rows are shared across API calls already,
    and identical symbol sequences from different callchain ids collapse too.
    Empty results (no --cudabacktrace in the capture, or old schema) are not
    an error: ({}, []).
    """
    try:
        import sqlite3
    except ImportError:
        logger.warning(
            "Python was built without the sqlite3 module; nsys timeline stacks "
            "are unavailable (timeline still renders without backtraces)"
        )
        return {}, []

    corr_to_stack: dict = {}
    stacks: List[List[str]] = []
    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            required = {"CUPTI_ACTIVITY_KIND_RUNTIME", "CUDA_CALLCHAINS", "StringIds"}
            if not required.issubset(tables):
                logger.info(
                    "nsys sqlite export lacks callchain tables (missing %s); no stacks",
                    ",".join(sorted(required - tables)),
                )
                return {}, []
            corr_to_chain = dict(
                conn.execute(
                    "SELECT correlationId, callchainId FROM CUPTI_ACTIVITY_KIND_RUNTIME "
                    "WHERE callchainId IS NOT NULL"
                ).fetchall()
            )
            frame_rows = conn.execute(
                "SELECT c.id, c.stackDepth, COALESCE(s.value, '?'), COALESCE(m.value, '') "
                "FROM CUDA_CALLCHAINS c "
                "LEFT JOIN StringIds s ON s.id = c.symbol "
                "LEFT JOIN StringIds m ON m.id = c.module "
                "ORDER BY c.id, c.stackDepth"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("Failed to read callchains from %s: %s", sqlite_path, exc)
        return {}, []

    chain_frames: dict = {}
    for chain_id, depth, symbol, module in frame_rows:
        frames = chain_frames.setdefault(chain_id, [])
        frames.append((depth, f"{symbol} ({module})" if module else str(symbol)))

    stack_index: dict = {}
    chain_to_stack: dict = {}
    for chain_id, frames in chain_frames.items():
        ordered = [f for _, f in sorted(frames, key=lambda x: x[0])]
        key = tuple(ordered)
        idx = stack_index.get(key)
        if idx is None:
            idx = len(stacks)
            stack_index[key] = idx
            stacks.append(ordered)
        chain_to_stack[chain_id] = idx

    for corr, chain_id in corr_to_chain.items():
        idx = chain_to_stack.get(chain_id)
        if idx is not None:
            corr_to_stack[int(corr)] = idx
    return corr_to_stack, stacks


def nsys_trace_to_timeline_events(
    nsys: Path, nsys_rep: Path, work_dir: Path, with_stacks: bool = False
) -> Optional[dict]:
    """Export cuda_gpu_trace + cuda_api_trace and parse them into timeline events.

    Returns {"gpu": [...], "api": [...], "stacks": [...]} (lists may be empty),
    or None if neither trace produced events. With with_stacks=True the report
    is also exported to SQLite and each event whose CorrID has a recorded CPU
    backtrace gets a "stack" index into the "stacks" table (GPU kernels resolve
    through the launching API call's CorrID).
    """
    work_dir.mkdir(parents=True, exist_ok=True)

    gpu_events: List[dict] = []
    api_events: List[dict] = []

    gpu_csv = _export_stats_csv(nsys, nsys_rep, work_dir, "cuda_gpu_trace", work_dir / "gpu_trace")
    if gpu_csv:
        gpu_events = _parse_trace_csv(gpu_csv, "gpu")
    api_csv = _export_stats_csv(nsys, nsys_rep, work_dir, "cuda_api_trace", work_dir / "api_trace")
    if api_csv:
        api_events = _parse_trace_csv(api_csv, "api")

    if not gpu_events and not api_events:
        logger.error("No usable nsys trace CSV (cuda_gpu_trace / cuda_api_trace)")
        return None

    stacks: List[List[str]] = []
    if with_stacks:
        sqlite_path = nsys_export_sqlite(nsys, nsys_rep, work_dir)
        if sqlite_path is not None:
            corr_to_stack, stacks = load_callchains_from_sqlite(sqlite_path)
            if corr_to_stack:
                for ev in api_events + gpu_events:
                    if ev["corr"] is not None:
                        ev["stack"] = corr_to_stack.get(ev["corr"], -1)
            logger.info(
                "Attached CPU backtraces: %d distinct stacks over %d CorrIDs",
                len(stacks),
                len(corr_to_stack),
            )
        if not stacks:
            logger.warning(
                "nsys timeline stacks requested but the SQLite export has no callchains "
                "(capture without --cudabacktrace, or unwinding produced nothing)"
            )

    logger.info(
        "Parsed nsys timeline traces: %d GPU events, %d CUDA API events", len(gpu_events), len(api_events)
    )
    return {"gpu": gpu_events, "api": api_events, "stacks": stacks}


def generate_nsys_timeline_html(events: dict, title: str = "nsys CPU/GPU timeline") -> Optional[str]:
    """Render timeline events as a self-contained HTML swim-lane view.

    Lanes: one per CPU thread issuing CUDA API calls, one per GPU device/stream.
    CorrID links a CPU-side launch to the GPU kernel it produced (click to
    highlight). When events carry a "stack" index into events["stacks"]
    (--cudabacktrace capture), clicking also opens a panel with the CPU
    backtrace that issued the launch. No external assets; suitable for the
    Studio Adhoc iframe.
    """
    import json

    all_events: List[dict] = []
    for kind in ("api", "gpu"):
        for ev in events.get(kind, []):
            all_events.append({**ev, "kind": kind})
    if not all_events:
        return None

    total_events = len(all_events)
    truncated = total_events > MAX_TIMELINE_EVENTS
    if truncated:
        all_events.sort(key=lambda e: -e["dur"])
        all_events = all_events[:MAX_TIMELINE_EVENTS]

    t0 = min(e["start"] for e in all_events)
    span = max(1, max(e["start"] + e["dur"] for e in all_events) - t0)
    all_events.sort(key=lambda e: e["start"])

    # CPU lanes first, then GPU lanes, each sorted by name for stable order.
    lane_names = sorted({e["lane"] for e in all_events if e["kind"] == "api"}) + sorted(
        {e["lane"] for e in all_events if e["kind"] == "gpu"}
    )
    lane_index = {name: i for i, name in enumerate(lane_names)}

    name_table: List[str] = []
    name_index: dict = {}
    packed = []
    for e in all_events:
        idx = name_index.get(e["name"])
        if idx is None:
            idx = len(name_table)
            name_index[e["name"]] = idx
            name_table.append(e["name"])
        corr = e["corr"] if e["corr"] is not None else -1
        stack = e.get("stack", -1)
        packed.append([e["start"] - t0, e["dur"], lane_index[e["lane"]], corr, idx, stack])

    data = {
        "lanes": lane_names,
        "cpuLanes": sum(1 for n in lane_names if n.startswith("CPU")),
        "names": name_table,
        "events": packed,
        "stacks": events.get("stacks") or [],
        "span": span,
        "total": total_events,
        "shown": len(all_events),
    }
    data_json = json.dumps(data, separators=(",", ":"))

    template = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 16px; background: #0b1020; color: #e8ecf5; }
h1 { font-size: 1.1rem; font-weight: 600; margin: 0 0 4px 0; }
.meta { color: #9aa3b5; font-size: 0.8rem; margin-bottom: 10px; }
#wrap { position: relative; background: #151b2e; border-radius: 8px; padding: 8px; }
#mini { display: block; width: 100%; height: 36px; cursor: grab; margin-bottom: 6px; }
#tl { display: block; width: 100%; cursor: crosshair; }
.lg { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin: 0 3px -1px 8px; }
#tip, #fgtip { position: absolute; display: none; pointer-events: none; background: #222b45; color: #e8ecf5;
  border: 1px solid #3a4568; border-radius: 4px; padding: 6px 8px; font-size: 0.75rem; max-width: 480px;
  white-space: pre-wrap; word-break: break-all; z-index: 10; }
#fgsec { display: none; }
#fgwrap { position: relative; background: #151b2e; border-radius: 8px; padding: 8px; }
#fg { display: block; width: 100%; cursor: pointer; }
#stack { display: none; background: #151b2e; border-radius: 8px; padding: 10px 12px; margin-top: 8px;
  font-size: 0.75rem; }
#stack h2 { font-size: 0.8rem; font-weight: 600; margin: 0 0 6px 0; word-break: break-all; }
#stack ol { margin: 0; padding-left: 22px; font-family: ui-monospace, monospace; }
#stack li { color: #c5cde0; word-break: break-all; padding: 1px 0; }
#stack .none { color: #9aa3b5; }
</style></head><body>
<h1>__TITLE__</h1>
<p class="meta" id="meta"></p>
<div id="wrap"><canvas id="mini"></canvas><canvas id="tl"></canvas><div id="tip"></div></div>
<div id="stack"></div>
<div id="fgsec">
<h1 style="margin-top:14px">Launch-stack flamegraph</h1>
<p class="meta" id="fgmeta"></p>
<div id="fgwrap"><canvas id="fg"></canvas><div id="fgtip"></div></div>
</div>
<script>
const DATA = __DATA__;
const LANE_H = 22, LABEL_W = 190, AXIS_H = 18;
const canvas = document.getElementById('tl'), tip = document.getElementById('tip');
let viewStart = 0, viewSpan = DATA.span, selCorr = -1;
const heightPx = AXIS_H + DATA.lanes.length * LANE_H;
function fmtNs(ns) {
  if (ns >= 1e9) return (ns / 1e9).toFixed(3) + ' s';
  if (ns >= 1e6) return (ns / 1e6).toFixed(3) + ' ms';
  if (ns >= 1e3) return (ns / 1e3).toFixed(1) + ' us';
  return ns + ' ns';
}
// Open auto-zoomed so the median event is a few pixels wide; at full span
// most captures collapse into a solid smear. "0" or the button resets.
let autoZoomed = false;
function initView() {
  const durs = DATA.events.map((e) => e[1]).filter((d) => d > 0).sort((a, b) => a - b);
  if (!durs.length) return;
  const med = durs[Math.floor(durs.length / 2)];
  const plotW = (canvas.clientWidth || 800) - LABEL_W;
  const target = Math.max(1000, med * plotW / 6);
  if (target < DATA.span * 0.9) {
    viewSpan = target;
    viewStart = Math.max(0, (DATA.span - viewSpan) / 2);
    autoZoomed = true;
  }
}
function fullSpan() { viewStart = 0; viewSpan = DATA.span; draw(); }
initView();
document.getElementById('meta').innerHTML =
  '<span class="lg" style="background:#5c8ae6"></span>CPU lanes: CUDA API calls per thread (a launch is the ' +
  'CPU asking for work).<span class="lg" style="background:#e6a15c"></span>GPU lanes: kernels/memcpys per ' +
  'stream (the work itself, usually later and longer). Each launch and its kernel share a correlation ID, ' +
  'so clicking either <span class="lg" style="background:#f5e663"></span>highlights both' +
  (DATA.stacks.length ? ' and shows the CPU backtrace of the launch' : '') +
  '.<br>Span ' + fmtNs(DATA.span) + ', ' + DATA.shown + ' of ' + DATA.total + ' events' +
  (DATA.shown < DATA.total ? ' (longest kept)' : '') +
  '. Drag on the overview strip to jump anywhere. Wheel or +/- keys: zoom' +
  ' - drag or arrow keys: pan - n/p keys: select next/previous event in view - 0: ' +
  '<button id="fit" style="font: inherit; background: #222b45; color: #e8ecf5; border: 1px solid #3a4568;' +
  ' border-radius: 4px; cursor: pointer; padding: 1px 8px;">Full span</button>' +
  (autoZoomed ? '. Opened auto-zoomed to the middle of the capture; sub-pixel events fade by lane occupancy.' : '.');
document.getElementById('fit').addEventListener('click', fullSpan);
function draw() {
  const cssW = canvas.clientWidth || 800;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = cssW * dpr; canvas.height = heightPx * dpr;
  canvas.style.height = heightPx + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, heightPx);
  const plotW = cssW - LABEL_W;
  ctx.font = '11px ui-sans-serif, system-ui, sans-serif';
  // lane labels + separators
  for (let i = 0; i < DATA.lanes.length; i++) {
    const y = AXIS_H + i * LANE_H;
    ctx.fillStyle = i % 2 ? '#181f36' : '#151b2e';
    ctx.fillRect(LABEL_W, y, plotW, LANE_H);
    ctx.fillStyle = '#9aa3b5';
    ctx.fillText(DATA.lanes[i], 4, y + LANE_H - 7, LABEL_W - 8);
  }
  // time axis ticks
  ctx.fillStyle = '#9aa3b5';
  const ticks = 6;
  for (let i = 0; i <= ticks; i++) {
    const t = viewStart + viewSpan * i / ticks;
    const x = LABEL_W + plotW * i / ticks;
    ctx.fillText(fmtNs(t), Math.min(x, cssW - 60), 12);
  }
  // events
  // Sub-pixel events don't get a solid 1px bar each (thousands of them tile
  // into a misleading solid strip); instead they accumulate per-pixel
  // occupancy and the column is shaded by how busy the lane actually is.
  const pxT = viewSpan / plotW; // time units per pixel
  const density = DATA.lanes.map(() => new Float32Array(Math.max(1, Math.ceil(plotW))));
  for (const [s, d, lane, corr, nameIdx] of DATA.events) {
    if (s + d < viewStart || s > viewStart + viewSpan) continue;
    const x = LABEL_W + (s - viewStart) / viewSpan * plotW;
    const w = d / viewSpan * plotW;
    const y = AXIS_H + lane * LANE_H + 3;
    const isSel = selCorr >= 0 && corr === selCorr;
    if (w < 1 && !isSel) {
      const col = Math.min(density[lane].length - 1, Math.max(0, Math.floor(x - LABEL_W)));
      density[lane][col] = Math.min(1, density[lane][col] + Math.max(0.05, w));
      continue;
    }
    if (isSel) ctx.fillStyle = '#f5e663';
    else ctx.fillStyle = lane < DATA.cpuLanes ? '#5c8ae6' : '#e6a15c';
    ctx.fillRect(x, y, Math.max(1, w), LANE_H - 6);
  }
  for (let lane = 0; lane < density.length; lane++) {
    const col = density[lane];
    const y = AXIS_H + lane * LANE_H + 3;
    const rgb = lane < DATA.cpuLanes ? '92,138,230' : '230,161,92';
    for (let i = 0; i < col.length; i++) {
      if (col[i] <= 0) continue;
      ctx.fillStyle = 'rgba(' + rgb + ',' + (0.25 + 0.75 * col[i]).toFixed(2) + ')';
      ctx.fillRect(LABEL_W + i, y, 1, LANE_H - 6);
    }
  }
  drawMini();
}
// Overview strip: full-capture density with a draggable viewport window, so
// you always see where you are and can jump without scroll-zooming out first.
const mini = document.getElementById('mini');
const MINI_H = 36;
let miniDensity = null;  // per-pixel [cpu, gpu] occupancy over the full span, cached per width
function drawMini() {
  const cssW = mini.clientWidth || 800;
  const dpr = window.devicePixelRatio || 1;
  mini.width = cssW * dpr; mini.height = MINI_H * dpr;
  mini.style.height = MINI_H + 'px';
  const ctx = mini.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = '#10152a';
  ctx.fillRect(0, 0, cssW, MINI_H);
  if (!miniDensity || miniDensity.cpu.length !== cssW) {
    miniDensity = { cpu: new Float32Array(cssW), gpu: new Float32Array(cssW) };
    for (const [s, d, lane] of DATA.events) {
      const a = Math.max(0, Math.floor(s / DATA.span * cssW));
      const b = Math.min(cssW - 1, Math.floor((s + d) / DATA.span * cssW));
      const arr = lane < DATA.cpuLanes ? miniDensity.cpu : miniDensity.gpu;
      for (let i = a; i <= b; i++) arr[i] = Math.min(1, arr[i] + 0.15);
    }
  }
  const half = (MINI_H - 6) / 2;
  for (let i = 0; i < cssW; i++) {
    if (miniDensity.cpu[i] > 0) {
      ctx.fillStyle = 'rgba(92,138,230,' + (0.3 + 0.7 * miniDensity.cpu[i]).toFixed(2) + ')';
      ctx.fillRect(i, 3, 1, half);
    }
    if (miniDensity.gpu[i] > 0) {
      ctx.fillStyle = 'rgba(230,161,92,' + (0.3 + 0.7 * miniDensity.gpu[i]).toFixed(2) + ')';
      ctx.fillRect(i, 3 + half, 1, half);
    }
  }
  const vx = viewStart / DATA.span * cssW;
  const vw = Math.max(3, viewSpan / DATA.span * cssW);
  ctx.strokeStyle = '#f5e663';
  ctx.lineWidth = 1.5;
  ctx.strokeRect(vx + 0.75, 1, vw - 1.5, MINI_H - 2);
  ctx.fillStyle = 'rgba(245,230,99,0.12)';
  ctx.fillRect(vx, 1, vw, MINI_H - 2);
}
function miniJump(clientX) {
  const r = mini.getBoundingClientRect();
  const frac = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
  viewStart = Math.min(Math.max(0, frac * DATA.span - viewSpan / 2), DATA.span - viewSpan);
  draw();
}
let miniDrag = false;
mini.addEventListener('mousedown', (e) => { miniDrag = true; miniJump(e.clientX); });
window.addEventListener('mousemove', (e) => { if (miniDrag) miniJump(e.clientX); });
window.addEventListener('mouseup', () => { miniDrag = false; });
mini.addEventListener('wheel', (e) => {
  e.preventDefault();
  viewSpan = Math.min(DATA.span, Math.max(1000, viewSpan * (e.deltaY > 0 ? 1.25 : 0.8)));
  viewStart = Math.min(Math.max(0, viewStart), DATA.span - viewSpan);
  draw();
}, { passive: false });
function hit(mx, my) {
  const plotW = (canvas.clientWidth || 800) - LABEL_W;
  if (mx < LABEL_W || my < AXIS_H) return null;
  const lane = Math.floor((my - AXIS_H) / LANE_H);
  const t = viewStart + (mx - LABEL_W) / plotW * viewSpan;
  const minW = viewSpan / plotW; // 1px in time units
  let best = null;
  for (const ev of DATA.events) {
    if (ev[2] !== lane) continue;
    if (t >= ev[0] && t <= ev[0] + Math.max(ev[1], minW)) best = ev;
  }
  return best;
}
canvas.addEventListener('mousemove', (e) => {
  const r = canvas.getBoundingClientRect();
  const ev = hit(e.clientX - r.left, e.clientY - r.top);
  if (ev) {
    tip.style.display = 'block';
    tip.style.left = (e.clientX - r.left + 12) + 'px';
    tip.style.top = (e.clientY - r.top + 12) + 'px';
    tip.textContent = DATA.names[ev[4]] + '\\nstart ' + fmtNs(ev[0]) + '  dur ' + fmtNs(ev[1]) +
      (ev[3] >= 0 ? '\\nCorrID ' + ev[3] : '');
  } else {
    tip.style.display = 'none';
  }
});
canvas.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
const stackEl = document.getElementById('stack');
function showStack(ev) {
  if (!ev || !DATA.stacks.length) { stackEl.style.display = 'none'; return; }
  stackEl.textContent = '';
  const h = document.createElement('h2');
  h.textContent = DATA.names[ev[4]] + (ev[3] >= 0 ? ' - CorrID ' + ev[3] : '') +
    (ev[2] >= DATA.cpuLanes ? ' (stack of the CPU launch)' : '');
  stackEl.appendChild(h);
  const frames = ev[5] >= 0 ? DATA.stacks[ev[5]] : null;
  if (frames && frames.length) {
    const ol = document.createElement('ol');
    for (const f of frames) {
      const li = document.createElement('li');
      li.textContent = f;
      ol.appendChild(li);
    }
    stackEl.appendChild(ol);
  } else {
    const p = document.createElement('div');
    p.className = 'none';
    p.textContent = 'No backtrace recorded for this event (below the --cudabacktrace ' +
      'threshold, or not a kernel launch).';
    stackEl.appendChild(p);
  }
  stackEl.style.display = 'block';
}
canvas.addEventListener('click', (e) => {
  const r = canvas.getBoundingClientRect();
  const ev = hit(e.clientX - r.left, e.clientY - r.top);
  const deselect = !ev || (ev[3] >= 0 && ev[3] === selCorr);
  selCorr = ev && ev[3] >= 0 && !deselect ? ev[3] : -1;
  showStack(deselect ? null : ev);
  draw();
});
canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  const r = canvas.getBoundingClientRect();
  const plotW = (canvas.clientWidth || 800) - LABEL_W;
  const mx = e.clientX - r.left - LABEL_W;
  if (mx < 0) return;
  const t = viewStart + mx / plotW * viewSpan;
  const factor = e.deltaY > 0 ? 1.25 : 0.8;
  viewSpan = Math.min(DATA.span, Math.max(1000, viewSpan * factor));
  viewStart = Math.min(Math.max(0, t - mx / plotW * viewSpan), DATA.span - viewSpan);
  draw();
}, { passive: false });
let dragX = null;
canvas.addEventListener('mousedown', (e) => { dragX = e.clientX; });
window.addEventListener('mouseup', () => { dragX = null; });
window.addEventListener('mousemove', (e) => {
  if (dragX === null) return;
  const plotW = (canvas.clientWidth || 800) - LABEL_W;
  viewStart = Math.min(Math.max(0, viewStart - (e.clientX - dragX) / plotW * viewSpan), DATA.span - viewSpan);
  dragX = e.clientX;
  draw();
});
// Aggregate launch-stack flamegraph: every backtraced event contributes its
// duration to its stack's frames (outermost at the top). GPU-lane events are
// preferred (widths = GPU kernel time); if none carry stacks, CPU launch
// durations are used instead.
const FG = (() => {
  if (!DATA.stacks.length) return null;
  let evs = DATA.events.filter((e) => e[5] >= 0 && e[2] >= DATA.cpuLanes);
  const weightKind = evs.length ? 'GPU kernel time' : 'CPU launch time';
  if (!evs.length) evs = DATA.events.filter((e) => e[5] >= 0);
  if (!evs.length) return null;
  const root = { name: 'all', value: 0, children: new Map() };
  for (const ev of evs) {
    const frames = DATA.stacks[ev[5]];
    if (!frames || !frames.length) continue;
    root.value += ev[1];
    let node = root;
    for (let i = frames.length - 1; i >= 0; i--) {  // outermost first
      let child = node.children.get(frames[i]);
      if (!child) { child = { name: frames[i], value: 0, children: new Map() }; node.children.set(frames[i], child); }
      child.value += ev[1];
      node = child;
    }
    // leaf: the kernel/API name itself, so different kernels from one call site split
    const leafName = DATA.names[ev[4]];
    let leaf = node.children.get(leafName);
    if (!leaf) { leaf = { name: leafName, value: 0, children: new Map() }; node.children.set(leafName, leaf); }
    leaf.value += ev[1];
  }
  return root.value > 0 ? { root, weightKind } : null;
})();
let fgFocus = FG ? FG.root : null;
let fgRects = [];  // {x, y, w, node, depth} in css px, rebuilt each draw
if (FG) {
  const FROW = 20;
  const fgCanvas = document.getElementById('fg'), fgTip = document.getElementById('fgtip');
  document.getElementById('fgsec').style.display = 'block';
  function depthOf(node) {
    let d = 1;
    for (const c of node.children.values()) d = Math.max(d, 1 + depthOf(c));
    return d;
  }
  const fgDepth = depthOf(FG.root);
  document.getElementById('fgmeta').textContent =
    'Same backtraces, aggregated: frame width = total ' + FG.weightKind +
    ' attributed to that call path (outermost frame on top, kernel name at the leaf). ' +
    'Click a frame to zoom into its subtree, click the root row to reset.';
  function fgColor(name, depth) {
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return 'hsl(' + (18 + (h % 42)) + ',' + (62 + (h >> 8) % 20) + '%,' + (46 + depth % 3 * 4) + '%)';
  }
  function fgDraw() {
    const cssW = fgCanvas.clientWidth || 800;
    const dpr = window.devicePixelRatio || 1;
    const cssH = fgDepth * FROW + FROW;
    fgCanvas.width = cssW * dpr; fgCanvas.height = cssH * dpr;
    fgCanvas.style.height = cssH + 'px';
    const ctx = fgCanvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    ctx.font = '11px ui-monospace, monospace';
    ctx.textBaseline = 'middle';
    fgRects = [];
    function rec(node, x, w, depth) {
      if (w < 0.5) return;
      const y = depth * FROW;
      ctx.fillStyle = depth ? fgColor(node.name, depth) : '#3a4568';
      ctx.fillRect(x, y + 1, Math.max(w - 0.5, 0.5), FROW - 2);
      if (w > 30) {
        ctx.fillStyle = '#10131f';
        ctx.save();
        ctx.beginPath(); ctx.rect(x + 3, y, w - 6, FROW); ctx.clip();
        ctx.fillText(depth ? node.name : 'all (' + fmtNs(fgFocus.value) + ')', x + 4, y + FROW / 2);
        ctx.restore();
      }
      fgRects.push({ x, y, w, node, depth });
      let cx = x;
      for (const c of node.children.values()) {
        const cw = w * c.value / node.value;
        rec(c, cx, cw, depth + 1);
        cx += cw;
      }
    }
    rec(fgFocus, 0, cssW, 0);
  }
  function fgHit(mx, my) {
    for (const r of fgRects) if (mx >= r.x && mx <= r.x + r.w && my >= r.y && my <= r.y + FROW) return r;
    return null;
  }
  fgCanvas.addEventListener('mousemove', (e) => {
    const b = fgCanvas.getBoundingClientRect();
    const r = fgHit(e.clientX - b.left, e.clientY - b.top);
    if (r) {
      fgTip.style.display = 'block';
      fgTip.style.left = (e.clientX - b.left + 12) + 'px';
      fgTip.style.top = (e.clientY - b.top + 12) + 'px';
      fgTip.textContent = r.node.name + '\\n' + fmtNs(r.node.value) + '  (' +
        (100 * r.node.value / FG.root.value).toFixed(1) + '% of all, ' +
        (100 * r.node.value / fgFocus.value).toFixed(1) + '% of view)';
    } else {
      fgTip.style.display = 'none';
    }
  });
  fgCanvas.addEventListener('mouseleave', () => { fgTip.style.display = 'none'; });
  fgCanvas.addEventListener('click', (e) => {
    const b = fgCanvas.getBoundingClientRect();
    const r = fgHit(e.clientX - b.left, e.clientY - b.top);
    fgFocus = r ? (r.depth === 0 ? FG.root : r.node) : FG.root;
    fgDraw();
  });
  window.addEventListener('resize', fgDraw);
  fgDraw();
}
// Keyboard: arrows pan, +/- zoom, n/p step through events in view (selecting
// each so its CorrID pair lights up and the stack panel follows), 0 resets.
let stepIdx = -1;
function selectEvent(ev) {
  selCorr = ev[3] >= 0 ? ev[3] : -1;
  showStack(ev);
  // keep the stepped event in view
  if (ev[0] < viewStart || ev[0] > viewStart + viewSpan) {
    viewStart = Math.min(Math.max(0, ev[0] - viewSpan / 2), DATA.span - viewSpan);
  }
  draw();
}
function step(dir) {
  const inView = [];
  for (let i = 0; i < DATA.events.length; i++) {
    const e = DATA.events[i];
    if (e[0] + e[1] >= viewStart && e[0] <= viewStart + viewSpan) inView.push(i);
  }
  if (!inView.length) return;
  const pos = inView.indexOf(stepIdx);
  stepIdx = inView[(pos + dir + inView.length) % inView.length];
  selectEvent(DATA.events[stepIdx]);
}
window.addEventListener('keydown', (e) => {
  if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  const panBy = viewSpan * 0.15;
  if (e.key === 'ArrowLeft') viewStart = Math.max(0, viewStart - panBy);
  else if (e.key === 'ArrowRight') viewStart = Math.min(DATA.span - viewSpan, viewStart + panBy);
  else if (e.key === '+' || e.key === '=') viewSpan = Math.max(1000, viewSpan * 0.7);
  else if (e.key === '-' || e.key === '_') viewSpan = Math.min(DATA.span, viewSpan * 1.4);
  else if (e.key === '0') { fullSpan(); return; }
  else if (e.key === 'n') { step(1); return; }
  else if (e.key === 'p') { step(-1); return; }
  else return;
  e.preventDefault();
  viewStart = Math.min(Math.max(0, viewStart), Math.max(0, DATA.span - viewSpan));
  draw();
});
window.addEventListener('resize', draw);
draw();
</script>
</body></html>
"""
    return template.replace("__TITLE__", title).replace("__DATA__", data_json)


def _simple_gpu_flamegraph_html(collapsed: str, title: str = "nsys GPU profile") -> str:
    """Minimal self-contained HTML for Adhoc iframe when burn/template unavailable."""
    rows = []
    total = 0
    parsed = []
    for line in collapsed.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            stack, count_s = line.rsplit(" ", 1)
            count = int(count_s)
        except ValueError:
            continue
        parsed.append((stack, count))
        total += count
    total = total or 1
    # Sort widest first (flamegraph convention)
    parsed.sort(key=lambda x: -x[1])
    bars = []
    for stack, count in parsed:
        pct = 100.0 * count / total
        leaf = stack.split(";")[-1]
        bars.append(
            f'<div class="bar" style="width:{pct:.2f}%" title="{stack} ({count})">'
            f"<span>{leaf}</span><em>{pct:.1f}%</em></div>"
        )
    bars_html = "\n".join(bars) if bars else "<p>No GPU samples</p>"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 24px; background: #0b1020; color: #e8ecf5; }}
h1 {{ font-size: 1.1rem; font-weight: 600; }}
.meta {{ color: #9aa3b5; font-size: 0.85rem; margin-bottom: 16px; }}
.frame {{ background: #151b2e; border-radius: 8px; padding: 12px; }}
.bar {{ background: linear-gradient(90deg,#c45c26,#e6a15c); color: #1a1208;
  margin: 4px 0; padding: 6px 10px; border-radius: 4px; box-sizing: border-box;
  min-width: 2px; display: flex; justify-content: space-between; gap: 8px;
  font-size: 0.8rem; overflow: hidden; white-space: nowrap; }}
.bar em {{ font-style: normal; opacity: 0.85; }}
</style></head><body>
<h1>{title}</h1>
<p class="meta">Collapsed stacks from NVIDIA nsys (cuda_gpu_kern_sum / cuda_api_sum).
Total weight: {total}. Inspired by GPU flame-graph views; host-detect nsys (not bundled).</p>
<div class="frame">{bars_html}</div>
</body></html>
"""


def generate_nsys_flamegraph_html(
    collapsed: str,
    start_iso: str,
    end_iso: str,
    generate_html_fn: Optional[Callable[..., Optional[str]]] = None,
) -> Optional[str]:
    """Turn collapsed stacks into flamegraph HTML via callback or simple fallback."""
    if generate_html_fn is not None:
        try:
            html = generate_html_fn(collapsed)
            if html:
                return html
        except Exception as exc:
            logger.warning("generate_html_fn failed (%s); using simple GPU HTML fallback", exc)

    # Prefer burn+template when the full agent runtime is available.
    try:
        from gprofiler.utils import resource_path, run_process, get_iso8601_format_time
        from datetime import datetime as _dt

        start = _dt.fromisoformat(start_iso.replace("Z", "+00:00")) if start_iso else _dt.utcnow()
        end = _dt.fromisoformat(end_iso.replace("Z", "+00:00")) if end_iso else _dt.utcnow()

        start_ts = get_iso8601_format_time(start)
        end_ts = get_iso8601_format_time(end)
        html = (
            Path(resource_path("flamegraph/flamegraph_template.html"))
            .read_bytes()
            .replace(
                b"{{{JSON_DATA}}}",
                run_process(
                    [resource_path("burn"), "convert", "--type=folded"],
                    suppress_log=True,
                    stdin=collapsed.encode(),
                    stop_event=None,
                    timeout=30,
                ).stdout,
            )
            .replace(b"{{{START_TIME}}}", start_ts.encode())
            .replace(b"{{{END_TIME}}}", end_ts.encode())
        )
        return html.decode("utf-8")
    except Exception as exc:
        logger.info("burn/template HTML unavailable (%s); using simple GPU HTML fallback", exc)
        return _simple_gpu_flamegraph_html(collapsed)


def without_nsys_perf_event_tags(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Return metadata whose run_arguments.perf_events drops the nsys-* tags.

    Studio labels an Adhoc row GPU from those tags, so an upload that fell back
    to the CPU flamegraph must not carry them. The input is shared static
    metadata, so it is copied rather than mutated.
    """
    run_args = metadata.get("run_arguments")
    if not isinstance(run_args, dict) or not run_args.get("perf_events"):
        return metadata
    events = [e.strip() for e in str(run_args["perf_events"]).split(",")]
    kept = [e for e in events if e and not e.startswith("nsys-")]
    return {**metadata, "run_arguments": {**run_args, "perf_events": ",".join(kept)}}


def collect_nsys_adhoc_html(
    *,
    duration_sec: int,
    nsys_path: Optional[str] = None,
    workload: Optional[str | Sequence[str]] = None,
    work_dir: Optional[str] = None,
    stop_event=None,
    generate_html_fn: Optional[Callable[[str], Optional[str]]] = None,
    timeline: bool = False,
    timeline_stacks: bool = False,
    on_rep: Optional[Callable[[Path], None]] = None,
    capture_mode: str = "launch",
    session_name: Optional[str] = None,
) -> Optional[str]:
    """End-to-end: find nsys → capture → collapsed → HTML. Returns HTML or None.

    With timeline=True the same capture is exported as cuda_gpu_trace +
    cuda_api_trace and rendered as a CPU/GPU timeline instead of a flamegraph
    (falling back to the flamegraph if the trace export yields no events).
    timeline_stacks=True additionally captures CPU backtraces per kernel launch
    (--cudabacktrace; heavier) and shows them on click in the timeline.
    on_rep is called with the .nsys-rep path right after a successful capture,
    so callers can keep/upload the raw report alongside the rendered HTML.
    """
    nsys = find_nsys(nsys_path)
    if nsys is None:
        logger.error(
            "enable_nsys set but nsys not found (install Nsight Systems on the host "
            "or set NSYS_PATH / --nsys-path). nsys is not bundled with gProfiler."
        )
        return None

    name = resolve_nsys_session_name(session_name)
    logger.info("Using nsys at %s", nsys)
    if workload:
        logger.info(
            "Ignoring nsys_workload %r; the client owns nsys launch. Attaching to session %s",
            workload,
            name,
        )
    base = Path(work_dir or NSYS_DATA_DIRECTORY)
    base.mkdir(parents=True, exist_ok=True)
    if capture_mode != "launch":
        logger.warning("nsys capture mode %r is not supported; attaching to session %s", capture_mode, name)
    return collect_nsys_launch_html(
        nsys=nsys,
        duration_sec=duration_sec,
        work_dir=base,
        stop_event=stop_event,
        timeline=timeline,
        timeline_stacks=timeline_stacks,
        on_rep=on_rep,
        generate_html_fn=generate_html_fn,
        session_name=name,
    )
