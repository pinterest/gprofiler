# Concurrent Profiling Constraints: Why Continuous + Ad-hoc Cannot Run Simultaneously

This document explains why gProfiler cannot run continuous and ad-hoc profiling at the same time, the technical constraints at each layer, and how the current queue-based design safely handles this.

## Overview

gProfiler enforces a **single-profiler-at-a-time** model. When both continuous and ad-hoc profiling are requested, the system uses a priority queue with pause/resume semantics rather than running them in parallel. This is not a design limitation that can be "fixed" -- it is the correct behavior dictated by fundamental constraints in the underlying profiling tools and kernel interfaces.

## Three Layers of Exclusivity

Concurrent profiling is blocked at three independent levels. Even if the upper layers were removed, the bottom layer (profiler internals) makes true parallelism impossible for the same target processes.

```
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 1: gProfiler System Mutex                                    │
│  grab_gprofiler_mutex() — one gProfiler process per host            │
├─────────────────────────────────────────────────────────────────────┤
│  Layer 2: DynamicGProfilerManager — single current_gprofiler slot   │
├─────────────────────────────────────────────────────────────────────┤
│  Layer 3: Profiler Internals — kernel/runtime single-tenant limits  │
│  (THIS IS THE HARD CONSTRAINT)                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Layer 1: System-Wide Mutex

`grab_gprofiler_mutex()` in `gprofiler/utils/__init__.py` uses a Unix domain socket in the abstract namespace of the init network namespace as a system-wide lock:

```python
def grab_gprofiler_mutex() -> bool:
    """
    Implements a basic, system-wide mutex for gProfiler, to make sure
    we don't run 2 instances simultaneously.
    """
    GPROFILER_LOCK = "\x00gprofiler_lock"
    try:
        run_in_ns_wrapper(["net"], lambda: try_acquire_mutex(GPROFILER_LOCK))
    except CouldNotAcquireMutex:
        return False
    else:
        return True
```

A second gProfiler process on the same host will fail to acquire this lock and exit immediately. This provides automatic cleanup when the process goes down (no stale lock files). You can check who holds the lock with:

```bash
sudo netstat -xp | grep gprofiler
```

**Could this be bypassed?** Technically yes, but it exists to prevent the exact resource conflicts described in Layer 3.

### Layer 2: Single-Slot Manager Design

`DynamicGProfilerManager` in `gprofiler/heartbeat.py` maintains a single `current_gprofiler` reference and a single `current_thread`:

```python
class DynamicGProfilerManager:
    def __init__(self, base_args, heartbeat_client):
        self.current_gprofiler: Optional['GProfiler'] = None
        self.current_thread: Optional[threading.Thread] = None
        self.current_command: Optional[ProfilingCommand] = None
```

When an ad-hoc command arrives while continuous profiling is running, the manager **pauses** the continuous profiler (stops it), runs the ad-hoc command, and then the continuous profiler can be re-queued:

```python
if next_cmd.command_type == "start":
    if self._can_be_paused():
        self.command_manager.pause_command(self.current_command.command_id)
        self._stop_current_profiler()
    self._start_new_profiler(next_cmd.profiling_command, next_cmd.command_id)
```

**Could this be refactored to hold two slots?** Yes, architecturally. But it would not help because of Layer 3.

### Layer 3: Profiler Internals (The Hard Constraint)

This is the fundamental blocker. The underlying profiling tools and kernel interfaces are **single-tenant per target process**. Two profiler instances targeting the same process will collide regardless of how the management layer is designed.

#### Java async-profiler: One Agent Per JVM

async-profiler is loaded as a native JVMTI agent into the target JVM via `jattach`. The JVM allows only one active async-profiler session at a time. A second `start` command returns an error:

```
[ERROR] Profiler already started
```

This is handled in `gprofiler/profilers/java.py`:

```python
def start_async_profiler(self, interval, second_try=False, ap_timeout=0):
    try:
        self._run_async_profiler(start_cmd)
        return True
    except JattachException as e:
        if e.is_ap_loaded:
            if (e.returncode == 200  # AP's COMMAND_ERROR
                and "[ERROR] Profiler already started\n" in e.get_ap_log()):
                return False  # profiler was already running
        raise
```

**Why this limitation exists:** async-profiler uses a single signal handler (for `perf_event` or `SIGPROF`) per JVM process. Two profiler sessions would need to share or fight over this single handler, which is not supported.

#### perf: Shared Hardware PMU Counters

The Linux `perf` subsystem uses hardware Performance Monitoring Unit (PMU) counters. These are finite physical resources on the CPU (typically 4-8 general-purpose counters per core). Two independent `perf record` sessions targeting the same processes or running system-wide would:

1. **Compete for PMU counters** -- the kernel multiplexes when counters are exhausted, reducing accuracy for both sessions
2. **Double the sampling overhead** -- each session generates its own interrupts and context switches
3. **Produce interleaved output** -- `perf record` writes to ring buffers; concurrent sessions can miss events during buffer processing
4. **Corrupt timing data** -- sampling rate regulation assumes a single consumer of the PMU events

The `PerfProcess` class in `gprofiler/utils/perf_process.py` configures `perf record` with specific mmap buffer sizes that assume exclusive access:

```python
class PerfProcess:
    _MMAP_SIZES = {"fp": 129, "dwarf": 257}  # pages, assumes exclusive access
```

#### Python py-spy / rbspy / phpspy: `ptrace` Exclusivity

py-spy, rbspy, and phpspy all use `ptrace()` to attach to their target processes. Linux enforces a strict **one tracer per process** rule:

```
ptrace(PTRACE_ATTACH, target_pid, ...) → EPERM if another tracer is attached
```

This is a kernel-level constraint (`kernel/ptrace.c`). A process can have at most one ptracer at any time. A second profiler attempting to `ptrace_attach` to an already-traced process will receive `EPERM`.

#### Python PyPerf (eBPF): Shared Kernel Resources

PyPerf loads eBPF programs into the kernel and attaches uprobes to Python interpreter functions. While the eBPF subsystem can theoretically support multiple programs, two PyPerf instances would:

- Attach duplicate uprobes to the same functions, doubling instrumentation overhead
- Write to separate eBPF maps, producing duplicate/divergent data
- Potentially conflict on perf event file descriptors

## Summary: Constraint Matrix

| Profiler | Attachment Method | Concurrency Limit | Enforcement Level |
|---|---|---|---|
| **async-profiler** (Java) | JVMTI agent via jattach | 1 per JVM process | JVM runtime (`[ERROR] Profiler already started`) |
| **perf** (system-wide) | `perf_event_open` syscall | Shared PMU counters (4-8 per core) | CPU hardware + kernel multiplexing |
| **py-spy** (Python) | `ptrace()` attach | 1 tracer per process | Kernel (`EPERM` on second attach) |
| **PyPerf** (Python eBPF) | eBPF uprobes | Shared kernel probes | Kernel (duplicate probe overhead) |
| **rbspy** (Ruby) | `ptrace()` attach | 1 tracer per process | Kernel (`EPERM`) |
| **phpspy** (PHP) | `ptrace()` attach | 1 tracer per process | Kernel (`EPERM`) |
| **dotnet-trace** (.NET) | EventPipe API | 1 session per process | .NET runtime (session exclusivity) |

## How the Queue System Handles This

The `CommandManager` in `gprofiler/command_control.py` implements a priority queue that safely serializes profiling commands:

```
Priority Order:
  1. Stop commands   (highest — immediate termination)
  2. Ad-hoc commands (single-run, higher than continuous)
  3. Continuous commands (long-running, lowest priority)
```

### Typical Flow: Ad-hoc Interrupts Continuous

```
Time ──────────────────────────────────────────────────────────►

 ┌──────────────────────┐
 │  Continuous profiling │  (running)
 │  command_id: cmd-001  │
 └──────────┬───────────┘
            │
            │  Ad-hoc command arrives (cmd-002)
            │  ┌──────────────────────────────────┐
            ▼  │                                  │
 1. Pause continuous (cmd-001 marked is_paused)   │
 2. Stop current profiler                         │
 3. Start ad-hoc profiler (cmd-002)               │
            │                                     │
            │  ┌──────────────┐                   │
            │  │  Ad-hoc run  │  (runs to completion)
            │  │  cmd-002     │                   │
            │  └──────┬───────┘                   │
            │         │                           │
            │  Ad-hoc completes, dequeued         │
            │  Heartbeat loop picks up next       │
            │  command from continuous queue       │
            │         │                           │
            │  ┌──────▼───────────────────┐       │
            │  │  Continuous resumes      │       │
            │  │  (new command or re-queue)│       │
            │  └──────────────────────────┘       │
            └─────────────────────────────────────┘
```

### Queue Sizing

```python
STOP_QUEUE_MAX_SIZE = 1       # Only one stop needed
ADHOC_QUEUE_MAX_SIZE = 10     # Buffer multiple ad-hoc requests
CONTINUOUS_QUEUE_MAX_SIZE = 1  # Only latest continuous config matters
```

When a new continuous command arrives, the continuous queue is cleared first (only the latest continuous configuration is relevant). Ad-hoc commands accumulate and are processed FIFO between heartbeat intervals.

## What Would Work (Theoretical Alternatives)

If you need both continuous monitoring and ad-hoc profiling to overlap in time, these approaches are possible within the existing constraints:

### 1. Time-Slicing (Current Design)

The pause/resume mechanism already provides this. Continuous profiling is interrupted briefly for ad-hoc snapshots, then resumes. The gap is typically the duration of a single ad-hoc snapshot (30-120 seconds).

**Trade-off:** Brief gap in continuous data during ad-hoc runs.

### 2. Non-Overlapping Profiler Types

If the continuous and ad-hoc commands targeted **different profiler types** (e.g., continuous runs perf-only for system overview, ad-hoc runs Java async-profiler only), they would not collide at Layer 3. This would require:

- Profiler-type-aware command routing in `DynamicGProfilerManager`
- Per-profiler-type locking instead of a single `current_gprofiler` slot
- Careful validation that the two commands don't share any profiler types

**Trade-off:** Significant architectural complexity; only helps when profiler types don't overlap.

### 3. Separate Hosts

Run continuous profiling on one set of replicas and send ad-hoc commands to a different set. No resource contention since different hosts have independent PMU counters, ptrace namespaces, and JVM processes.

**Trade-off:** Requires sufficient fleet size and intelligent routing in Performance Studio.

### 4. Sampling Rate Reduction

Run a single continuous profiler at a lower sampling rate, and temporarily increase the rate for "ad-hoc" snapshots. This stays within the single-profiler constraint while approximating the effect of two profilers.

**Trade-off:** Lower baseline continuous resolution; complexity in dynamic rate adjustment.

## Key Takeaways

1. **The single-profiler-at-a-time model is correct.** It is not a limitation to be worked around, but a reflection of real hardware and kernel constraints.

2. **The queue-based pause/resume design is the standard solution.** It gives ad-hoc commands higher priority while preserving continuous profiling continuity.

3. **True parallel profiling of the same processes is physically impossible** with the current generation of profiling tools (async-profiler, perf, ptrace-based profilers).

4. **The system-wide mutex (`grab_gprofiler_mutex`) exists as a safety net** to prevent accidental concurrent instances from causing the exact conflicts described above.
