# gProfiler Process Filtering Behavior

## Overview

When using **heartbeat command control** or **specific PID targeting** in gProfiler, you may notice that some profilers for other languages still run and attempt to profile processes, even though you're targeting a specific process. This behavior varies by profiler type due to gProfiler's mixed filtering architecture: some profilers now filter **before** profiling (efficient), while others still filter **after** profiling (legacy post-filtering behavior).

## What Happens During Profiling

### Timeline of Events

The filtering behavior depends on the profiler type:

#### **Individual Process Profilers** (Java, Ruby, .NET, Python py-spy)
1. **Start Phase**: Profiler starts and scans for target processes
2. **Filtering Phase**: **BEFORE profiling begins**, filter processes based on `processes_to_profile`
3. **Profiling Phase**: Profile only the filtered set of processes

#### **System-Wide Profilers** (Python eBPF, PHP, System/perf)
1. **Start Phase**: Profiler starts and begins scanning/profiling system-wide  
2. **Profiling Phase**: Profilers collect data from ALL processes they can detect (for the full duration)
3. **Filtering Phase**: **AFTER profiling**, filter out unwanted processes from results

### This means:

- **Individual process profilers** (Java, Ruby, .NET, py-spy): **Efficient** - only profile target processes
- **System-wide profilers** (Python eBPF, PHP, System/perf): **Less efficient** - profile all processes, then filter results

## Profiler Filtering Behavior

gProfiler profilers use two different filtering approaches:

### **POST-Profiling Filtering** (System-Wide Profilers)

These profilers collect data from all processes, then filter results afterwards:

#### 🐍 **Python eBPF Profiler** 
**Location**: `gprofiler/profilers/python_ebpf.py:373-375`

```python
# PyPerf profiles ALL Python processes system-wide for full duration
def snapshot(self) -> ProcessToProfileData:
    collapsed_path = self._dump()  # ← All Python processes profiled
    parsed = parse_many_collapsed(collapsed_text)  # ← Parse all results
    
    for pid in parsed:  # ← Iterate through ALL profiled PIDs
        process = Process(pid)
        # Filtering happens HERE - after profiling is complete
        if self._profiler_state.processes_to_profile is not None:
            if process not in self._profiler_state.processes_to_profile:
                continue  # ← Skip from final output only
```

**Impact**: PyPerf runs for full duration, profiles all Python processes, then filters results.

#### 🐘 **PHP Profiler**
**Location**: `gprofiler/profilers/php.py:211-213`

```python
# phpspy profiles detected PHP processes, then filters output
def _parse_phpspy_output(self, output: str, profiler_state: ProfilerState) -> ProcessToProfileData:
    # ... profiling work already done ...
    for pid in results:
        # Post-profiling filtering
        if profiler_state.processes_to_profile is not None:
            if pid not in [process.pid for process in profiler_state.processes_to_profile]:
                continue  # ← Skip from results
```

**Impact**: phpspy runs and profiles PHP processes, then filters output.

#### ⚡ **System Profiler (perf)**
**Location**: `gprofiler/profilers/perf.py:269`

```python
# perf receives processes_to_profile but still runs system-wide collection
def start(self) -> None:
    self._perf_fp = PerfProcess(
        # ...
        processes_to_profile=self._profiler_state.processes_to_profile,  # ← Passed to perf
    )
```

**Impact**: `perf record` may use `--pid` flag to focus collection, but still runs system-wide monitoring.

### **PRE-Profiling Filtering** (Individual Process Profilers)

These profilers filter target processes BEFORE starting profiling work:

#### 💎 **Ruby Profiler**
**Location**: `gprofiler/profilers/profiler_base.py:269-273` (base class filtering)

```python
# All ProcessProfilerBase profilers now filter BEFORE profiling
def snapshot(self) -> ProcessToProfileData:
    processes_to_profile = self._select_processes_to_profile()  # ← Find target processes
    if self._profiler_state.processes_to_profile is not None:
        processes_to_profile = [
            process for process in processes_to_profile 
            if process in self._profiler_state.processes_to_profile  # ← Filter BEFORE profiling
        ]
    # Only profile filtered processes
```

**Impact**: Ruby profiler filters BEFORE profiling (efficient).

#### ☕ **Java Profiler**  
**Location**: `gprofiler/profilers/profiler_base.py:269-273` (base class filtering)

**Impact**: Java profiler filters BEFORE profiling (efficient).

#### 🔷 **.NET Profiler**
**Location**: `gprofiler/profilers/profiler_base.py:269-273` (base class filtering)

**Impact**: .NET profiler filters BEFORE profiling (efficient).

#### 🐍 **Python py-spy Profiler**
**Location**: `gprofiler/profilers/profiler_base.py:269-273` (base class filtering)

**Impact**: py-spy profiler now filters BEFORE profiling (efficient).

## Why This Design Exists

### **System-Wide Profilers** (Python eBPF, PHP, System/perf)
- **Efficiency**: eBPF/kernel-level tools are more efficient when monitoring system-wide
- **Process Discovery**: Some processes may spawn during profiling
- **Technical Constraints**: Harder to filter at kernel/eBPF level
- **Architecture**: These inherit directly from `ProfilerBase`, not `ProcessProfilerBase`

### **Individual Process Profilers** (Java, Ruby, .NET, Python py-spy)  
- **Targeted Tools**: These tools naturally profile one process at a time
- **Early Filtering**: Can efficiently skip processes before starting profiling work
- **Architecture**: These inherit from `ProcessProfilerBase` or `SpawningProcessProfilerBase`, which provides automatic pre-filtering

## Impact on Performance

### **Efficient vs Wasteful Resource Usage**
When targeting a specific process via heartbeat or PID targeting:

#### **✅ Efficient Profilers** (Individual Process Profilers)
```
Example: Targeting Java PID 964466 via heartbeat
✅ Java Profiler: Profiles only PID 964466 (efficient - PRE-filtering)
✅ Ruby Profiler: Skips all Ruby processes (efficient - PRE-filtering)  
✅ .NET Profiler: Skips all .NET processes (efficient - PRE-filtering)
✅ Python py-spy: Skips all Python processes (efficient - PRE-filtering)
```

#### **❌ Wasteful Profilers** (System-Wide Profilers)
```
Example: Targeting Java PID 964466 via heartbeat  
❌ Python eBPF: Profiles ALL Python processes for 60s, then discards results
❌ System Profiler: Runs system-wide perf collection, then filters
❌ PHP Profiler: Scans for and profiles PHP processes, then discards
```

### **Resource Usage by System-Wide Profilers**
When system-wide profilers run unnecessarily:
- **CPU**: System-wide profiling overhead for unwanted processes
- **Memory**: Buffers for all processes of that language type
- **I/O**: Writing/reading profile data for processes that will be discarded
- **Time**: Full profiling duration spent on irrelevant processes

## Example Behavior When Targeting Specific PIDs

When targeting Java PID 964466 via heartbeat or `--processes-to-profile`:

```
[2025-08-15 21:17:37,908] INFO: gprofiler.profilers.java: Profiling process 964466 with async-profiler
# ✅ Java profiler (ProcessProfilerBase) correctly targets only PID 964466

[2025-08-15 21:17:37,910] DEBUG: gprofiler.profilers.python: Selected 0 processes to profile
[2025-08-15 21:17:37,910] DEBUG: gprofiler.profilers.python: processes left after filtering: 0
# ✅ Python py-spy (ProcessProfilerBase) efficiently skips all processes - no work done

[2025-08-15 21:18:37,943] DEBUG: gprofiler.profilers.python_ebpf: PyPerf dump output
# ❌ Python eBPF profiler (ProfilerBase) spent 60 seconds profiling ALL Python processes
# Then failed on Bazel processes with deleted libraries  
# Finally discarded all results since none matched PID 964466
```

## Solutions & Workarounds

### **Option 1: Disable Wasteful System-Wide Profilers**
```bash
gprofiler \
  --enable-heartbeat-server \
  --python-mode pyspy \      # Use py-spy instead of eBPF (efficient pre-filtering)
  --php-mode disabled \      # Disable PHP (system-wide profiler)
  --perf-mode disabled \     # Disable perf (system-wide profiler)
  # Java, Ruby, .NET profilers will efficiently target specific processes
```

### **Option 2: Use Direct PID Targeting**
```bash
gprofiler --processes-to-profile 964466 --java-mode ap
# All ProcessProfilerBase profilers automatically filter efficiently
# Only system-wide profilers (eBPF, PHP, perf) waste resources
```

### **Option 3: Accept the Overhead from System-Wide Profilers**
- Current behavior ensures comprehensive system coverage
- Final results are correctly filtered
- Individual process profilers are now efficient (pre-filtering)
- Only system-wide profilers waste resources (post-filtering)

## Architecture Improvement Opportunities

### **For System-Wide Profilers**
- **Early PID Filtering**: Check `processes_to_profile` before starting profiling
- **Conditional Startup**: Don't start profiler if no target processes match language type
- **Resource Optimization**: Reduce buffer sizes when targeting specific PIDs

### **Example Improvement**
```python
def start(self) -> None:
    # Check if we should even start
    if self._profiler_state.processes_to_profile is not None:
        target_pids = [p.pid for p in self._profiler_state.processes_to_profile]
        if not any(self._is_python_process(pid) for pid in target_pids):
            logger.info("No Python processes in target list, skipping Python profiler")
            return
    
    # Proceed with profiling
    logger.info("Starting profiling of Python processes with PyPerf")
    # ...
```

## Related Issues

- **GitHub Issue #764**: Python eBPF post-filtering (referenced in code)
- **GitHub Issue #763**: PHP post-filtering (referenced in code)

## Profiler Filtering Summary

### **POST-Profiling Filtering** (Less Efficient - Profiles All, Then Filters)
| Profiler | Class Hierarchy | Location | Behavior |
|----------|----------------|----------|----------|
| **🐍 Python eBPF** | `ProfilerBase` | `python_ebpf.py:373-375` | PyPerf profiles ALL Python processes system-wide, then filters results |
| **🐘 PHP** | `ProfilerBase` | `php.py:211-213` | phpspy profiles detected PHP processes, then filters output |
| **⚡ System/perf** | `ProfilerBase` | `perf.py:269` | perf runs system-wide collection, then filters results |

### **PRE-Profiling Filtering** (More Efficient - Filters First, Then Profiles)
| Profiler | Class Hierarchy | Location | Behavior |
|----------|----------------|----------|----------|
| **☕ Java** | `SpawningProcessProfilerBase` | `profiler_base.py:269-273` | Filters target processes BEFORE starting async-profiler |
| **💎 Ruby** | `SpawningProcessProfilerBase` | `profiler_base.py:269-273` | Filters target processes BEFORE starting rbspy |
| **🔷 .NET** | `ProcessProfilerBase` | `profiler_base.py:269-273` | Filters target processes BEFORE starting dotnet-trace |
| **🐍 Python py-spy** | `SpawningProcessProfilerBase` | `profiler_base.py:269-273` | Filters target processes BEFORE starting py-spy |

### **Resource Impact When Targeting Specific PIDs**
- **✅ Efficient**: Java, Ruby, .NET, Python py-spy profilers only work on target processes
- **❌ Wasteful**: Python eBPF, PHP, System profilers do unnecessary work then discard results

## Summary

**gProfiler now uses two different filtering approaches** depending on the profiler architecture:

### **✅ Efficient PRE-Profiling Filtering** 
All profilers inheriting from `ProcessProfilerBase` or `SpawningProcessProfilerBase` now automatically filter target processes **BEFORE** starting profiling work:
- **Java, Ruby, .NET, Python py-spy**: Only profile processes that match `processes_to_profile`
- **Resource Impact**: Minimal overhead when targeting specific PIDs

### **❌ Legacy POST-Profiling Filtering**
Profilers inheriting directly from `ProfilerBase` still collect data from all processes, then filter results afterwards:
- **Python eBPF, PHP, System/perf**: Profile all processes, then discard unwanted results  
- **Resource Impact**: Significant overhead when targeting specific PIDs

### **Recommendations**
- **For efficiency**: Use `--python-mode pyspy` instead of `pyperf` when targeting specific processes
- **Disable wasteful profilers**: Use `--php-mode disabled --perf-mode disabled` when not needed
- **Direct PID targeting**: Use `--processes-to-profile PID` for maximum efficiency

The gap between efficient and wasteful profilers has **significantly improved** with the base class pre-filtering implementation.
