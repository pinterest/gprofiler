
# Architecture & Theory of Operation

## 🏗️ gProfiler Agent Architecture

gProfiler is a multi-language profiler that combines system-wide profiling with runtime-specific profilers to provide comprehensive CPU profiling across different programming languages and environments.

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           gProfiler Agent Architecture                          │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  ┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────────────┐ │
│  │   Main Process  │    │  Process Scanner │    │    Profiler Factory         │ │
│  │   (main.py)     │ ── │  (Discovery)     │ ── │   (Creates Profilers)       │ │
│  │                 │    │                  │    │                             │ │
│  └─────────────────┘    └──────────────────┘    └─────────────────────────────┘ │
│           │                       │                            │                │
│           │                       │                            ▼                │
│           │              ┌────────▼─────────┐    ┌─────────────────────────────┐ │
│           │              │  Language        │    │     Profiler Registry       │ │
│           │              │  Detection       │    │   (Java, Python, Ruby,     │ │
│           │              │  (/proc/*/maps)  │    │    Go, Node.js, .NET)      │ │
│           │              └──────────────────┘    └─────────────────────────────┘ │
│           │                                                   │                │
│           ▼                                                   ▼                │
│  ┌─────────────────────────────────────────────────────────────────────────────┐ │
│  │                        Profiling Execution Engine                          │ │
│  └─────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Process Discovery & Language Detection

gProfiler automatically discovers running processes and identifies their programming language through two primary mechanisms:

#### 1. Memory Map Analysis (`/proc/*/maps`)

The primary detection method scans process memory maps for language-specific library signatures:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                      Process Discovery System                                  │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  System Scan Every 60s (configurable)                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                    Fast Memory Map Scanning                             │   │
│  │                                                                         │   │
│  │  $ grep -lE '^.+/libjvm\.so' /proc/*/maps          ── Java Detection   │   │
│  │  $ grep -lE 'libpython.*\.so' /proc/*/maps         ── Python Detection │   │
│  │  $ grep -lE '/ruby[^/]*$' /proc/*/maps             ── Ruby Detection   │   │
│  │                                                                         │   │
│  │  Performance: ~50ms to scan 1000+ processes using shell grep           │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                    Process Validation                                   │   │
│  │                                                                         │   │
│  │  • Double-check detection patterns                                      │   │
│  │  • Apply blacklists (avoid system processes)                           │   │
│  │  • Filter embedded runtimes (e.g., Python in other apps)               │   │
│  │  • Handle race conditions (processes dying during scan)                │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

#### 2. Executable Name Pattern Matching

Secondary detection method for languages without distinct library signatures:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    Executable-Based Detection                                   │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  Languages without unique library signatures:                                   │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                                                                         │   │
│  │  Node.js:  $ pgrep -f "node"                                           │   │
│  │            $ ps aux | grep ".*node[^/]*$"                               │   │
│  │                                                                         │   │
│  │  Go:       ELF binary analysis for Go build IDs                        │   │
│  │            $ file /proc/PID/exe | grep "Go BuildID"                     │   │
│  │                                                                         │   │
│  │  .NET:     $ pgrep -f "dotnet"                                          │   │
│  │            Pattern: ".*dotnet$"                                         │   │
│  │                                                                         │   │
│  │  PHP:      Process name filtering (configurable)                       │   │
│  │            Default: "php-fpm" processes                                 │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

#### Detection Method Comparison

| Language | Primary Method | Pattern | Reliability | Speed |
|----------|----------------|---------|-------------|-------|
| **Java** | `/proc/*/maps` | `libjvm.so` | Very High | Fast |
| **Python** | `/proc/*/maps` | `libpython*.so` | High | Fast |
| **Ruby** | `/proc/*/maps` | `/ruby[^/]*$` | High | Fast |
| **Go** | ELF Analysis | Build ID + symbols | Very High | Medium |
| **Node.js** | Process Name | `node` executable | Medium | Fast |
| **PHP** | Process Name | `php-fpm` (configurable) | Medium | Fast |
| **.NET** | Mixed | `dotnet` + libraries | Medium | Fast |

## 🔧 Individual Profiler Deep Dive

### Java Profiler (async-profiler)

**How it works:**
```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         Java async-profiler Architecture                       │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  1. Process Attachment                                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Uses JVM Tool Interface (JVMTI) - standardized JVM API                │   │
│  │  • Loads as native agent into target JVM process                        │   │
│  │  • No process interruption - runs inside JVM address space              │   │
│  │  • Command: java -jar async-profiler.jar -d 60 -f profile.html PID      │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  2. Sampling Mechanism (Multiple Modes)                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  async-profiler supports different sampling mechanisms:                  │   │
│  │                                                                         │   │
│  │  • CPU Mode (event=cpu): Uses perf_events for CPU-only profiling        │   │
│  │    - Only samples when thread is actively using CPU                     │   │
│  │    - Misses I/O waits, lock contention, sleep time                      │   │
│  │                                                                         │   │
│  │  • Wall Mode (event=wall): Internal timer for wall clock profiling     │   │
│  │    - Captures all time including I/O waits, blocking operations         │   │
│  │    - Best for identifying latency and blocking issues                   │   │
│  │                                                                         │   │
│  │  • Timer Mode (event=itimer): SIGPROF signals (fallback)               │   │
│  │    - Uses SIGPROF at configured frequency (default: 11Hz)               │   │
│  │    - Wall time profiling when perf_events unavailable                   │   │
│  │                                                                         │   │
│  │  All modes: Capture Java + native stacks, JIT stages (_[j], _[i])      │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  3. Stack Trace Collection                                                      │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Walks JVM internal stack structures                                  │   │
│  │  • Resolves method names using JVM symbol tables                        │   │
│  │  • Includes line numbers and inlining information                       │   │
│  │  • Handles both interpreted and compiled code paths                     │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  4. Output Format                                                               │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  com.app.Handler.process_[j] 25                                         │   │
│  │  com.app.Service.call_[i] 15                                            │   │
│  │  com.app.DB.query_[0] 8                                                 │   │
│  │                                                                         │   │
│  │  Suffixes: _[j]=JIT, _[i]=inlined, _[0]=interpreted, _[1]=C1 compiler  │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Python Profiler (py-spy)

**How it works:**
```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           Python py-spy Architecture                           │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  1. Process Attachment                                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Uses ptrace() system call to attach to Python process                │   │
│  │  • Reads process memory directly (/proc/PID/mem)                        │   │
│  │  • Requires CAP_SYS_PTRACE or same user ownership                       │   │
│  │  • Command: py-spy record -d 60 -f profile.svg -p PID                   │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  2. Python Runtime Inspection                                                   │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Locates Python interpreter structures in memory                      │   │
│  │  • Finds PyThreadState and PyFrameObject structures                     │   │
│  │  • Handles multiple Python versions (2.7, 3.5-3.12)                    │   │
│  │  • Adapts to different Python builds and configurations                 │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  3. Sampling & Stack Walking                                                    │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Pauses target process briefly (SIGSTOP/SIGCONT)                      │   │
│  │  • Walks Python call stack via frame pointers                           │   │
│  │  • Extracts function names, filenames, line numbers                     │   │
│  │  • Minimal interruption: ~100μs per sample                              │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  4. Output Format                                                               │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  main.py:main_[p] 30                                                    │   │
│  │  handler.py:process_request_[p] 20                                       │   │
│  │  db.py:query_[p] 15                                                      │   │
│  │                                                                         │   │
│  │  Format: filename.py:function_name_[p] sample_count                     │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Python Profiler (PyPerf - eBPF Alternative)

**How it works:**
```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           Python PyPerf (eBPF) Architecture                    │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  1. eBPF Program Loading                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Loads eBPF program into kernel                                       │   │
│  │  • No process attachment needed - kernel-level profiling                │   │
│  │  • Requires Linux 4.14+ and eBPF support                               │   │
│  │  • Uses uprobes and stack unwinding                                     │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  2. System-wide Python Detection                                                │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Automatically finds all Python processes                             │   │
│  │  • Instruments Python function calls via uprobes                       │   │
│  │  • Captures both Python and native stacks                               │   │
│  │  • Zero overhead when not profiling                                     │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  3. Stack Collection                                                             │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Collects stacks at timer intervals                                   │   │
│  │  • Uses kernel stack unwinding capabilities                             │   │
│  │  • More efficient than py-spy for system-wide profiling                │   │
│  │  • Lower overhead, higher accuracy                                      │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Ruby Profiler (rbspy)

**How it works:**
```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           Ruby rbspy Architecture                              │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  1. Process Attachment                                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Similar to py-spy, uses ptrace() system call                         │   │
│  │  • Attaches to Ruby process and reads memory                            │   │
│  │  • Supports Ruby versions 1.9.1 to 3.0+                                │   │
│  │  • Command: rbspy record -d 60 -f profile.svg -p PID                    │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  2. Ruby VM Inspection                                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Locates Ruby VM structures (rb_thread_t, rb_control_frame_t)         │   │
│  │  • Reads Ruby's internal call stack representation                      │   │
│  │  • Handles different Ruby implementations (MRI, YARV)                   │   │
│  │  • Adapts to Ruby version differences                                   │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  3. Stack Sampling                                                              │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Pauses Ruby process briefly for stack capture                        │   │
│  │  • Walks Ruby call frames                                               │   │
│  │  • Extracts method names, class names, file locations                   │   │
│  │  • Minimal process interruption                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  4. Output Format                                                               │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  User#authenticate_[rb] 25                                               │   │
│  │  Controller#process_request_[rb] 18                                      │   │
│  │  Database#query_[rb] 12                                                  │   │
│  │                                                                         │   │
│  │  Format: Class#method_[rb] sample_count                                 │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### .NET Profiler (dotnet-trace)

**How it works:**
```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           .NET dotnet-trace Architecture                       │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  1. EventPipe Attachment                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Uses .NET EventPipe - official .NET profiling API                    │   │
│  │  • Connects via named pipes or Unix domain sockets                      │   │
│  │  • Non-intrusive - no code injection required                           │   │
│  │  • Command: dotnet-trace collect -p PID -o profile.speedscope           │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  2. Event Stream Collection                                                     │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Subscribes to CLR runtime events                                     │   │
│  │  • Captures method entry/exit events                                    │   │
│  │  • Collects stack traces via sampling profiler                          │   │
│  │  • Handles both JIT and AOT compiled code                               │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  3. Stack Trace Processing                                                      │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Processes .NET runtime events into stack traces                      │   │
│  │  • Resolves method names using metadata                                 │   │
│  │  • Includes namespace and class information                              │   │
│  │  • Low overhead - built into .NET runtime                               │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  4. Output Format                                                               │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  MyApp.Controllers.UserController.GetUser_[net] 20                      │   │
│  │  MyApp.Services.UserService.FindById_[net] 15                           │   │
│  │  System.Data.SqlClient.SqlCommand.ExecuteReader_[net] 10                │   │
│  │                                                                         │   │
│  │  Format: Namespace.Class.Method_[net] sample_count                      │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### PHP Profiler (phpspy)

**How it works:**
```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           PHP phpspy Architecture                              │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  1. Process Attachment                                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Uses ptrace() to attach to PHP processes                             │   │
│  │  • Targets php-fpm processes by default                                 │   │
│  │  • Supports PHP 7.0-8.0+ (Zend Engine)                                 │   │
│  │  • Command: phpspy -p PID -d 60                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  2. PHP Engine Inspection                                                       │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Reads Zend Engine internal structures                                │   │
│  │  • Locates executor globals and call stack                              │   │
│  │  • Handles different PHP versions and configurations                    │   │
│  │  • Extracts function names and file information                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  3. Stack Sampling                                                              │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Samples PHP call stack at regular intervals                          │   │
│  │  • Pauses process briefly for stack capture                             │   │
│  │  • Walks PHP execution frames                                           │   │
│  │  • Minimal performance impact on target application                     │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  4. Output Format                                                               │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  UserController::authenticate_[php] 22                                   │   │
│  │  Database::query_[php] 18                                                │   │
│  │  Template::render_[php] 10                                               │   │
│  │                                                                         │   │
│  │  Format: Class::method_[php] sample_count                               │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### System Profiler (perf)

**How it works:**
```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           System perf Architecture                             │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  1. System-wide Profiling Setup                                                 │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Uses Linux perf_events subsystem                                     │   │
│  │  • Profiles ALL processes system-wide (-a flag)                         │   │
│  │  • Cannot cherry-pick processes reliably                                │   │
│  │  • Command: perf record -a -g -F 11 -o perf.data                       │   │
│  │  • Event discovery: tries 'cycles' (PMU), falls back to software      │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  2. Time-based Sampling Mechanism                                               │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Uses time-based sampling at 11Hz frequency (-F 11)                   │   │
│  │  • Preferred event: PMU 'cycles' (hardware counter)                     │   │
│  │  • Fallback events: 'cpu-clock', 'task-clock' (software timers)         │   │
│  │  • Captures instruction pointer and stack at each sample                │   │
│  │  • Works for any language/runtime (Go, Node.js, C++, etc.)             │   │
│  │                                                                         │   │
│  │  Note: This is time-based, NOT event-based PMU sampling                 │   │
│  │  (e.g., NOT "every N cache misses" but "every 1/11th second")          │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  3. Stack Unwinding                                                             │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Two modes: Frame Pointers (FP) or DWARF unwinding                    │   │
│  │  • FP: Fast, requires -fno-omit-frame-pointer                           │   │
│  │  • DWARF: Slower, works with optimized code                             │   │
│  │  • Smart mode: Tries both, picks better result                          │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  4. Symbol Resolution                                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Resolves addresses to function names using symbol tables             │   │
│  │  • Handles stripped binaries with external debug info                   │   │
│  │  • Special handling for Go (build IDs), Node.js (maps)                  │   │
│  │  • Kernel symbols via /proc/kallsyms                                     │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  5. Output Processing                                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  • Command: perf script -i perf.data                                    │   │
│  │  • Produces text output with stack traces                               │   │
│  │  • gProfiler parses and converts to collapsed format                    │   │
│  │  • Adds _[k] suffix for kernel functions                                │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Profiler Comparison Matrix

| Profiler | Language | Attachment Method | Interruption Type | Overhead | Accuracy |
|----------|----------|-------------------|-------------------|----------|----------|
| **async-profiler** | Java | JVMTI Agent | Multiple modes* | Very Low | Very High |
| **py-spy** | Python | ptrace() | Process pause | Low | High |
| **PyPerf** | Python | eBPF kernel | Hardware sampling | Very Low | Very High |
| **rbspy** | Ruby | ptrace() | Process pause | Low | High |
| **dotnet-trace** | .NET | EventPipe API | Event streaming | Very Low | High |
| **phpspy** | PHP | ptrace() | Process pause | Low | Medium |
| **perf** | All/Native | PMU/Software timers | Time-based sampling | Low | High |

*async-profiler modes: `cpu` (perf_events, CPU-only), `wall` (internal timer, includes I/O waits), `itimer` (SIGPROF signals, fallback)

### Wall Time Profiling Validation

A comprehensive proof-of-concept test application is available in `docs/java_profiler_wall_time_proof_of_concept/` that demonstrates the difference between CPU and wall time profiling. The test validates that wall time profiling successfully captures I/O waits, lock contention, and blocking operations invisible to CPU-only profiling.

### Current Perf Implementation vs PMU Event-based Profiling

**Current Implementation (Time-based Sampling):**
```bash
# What gprofiler currently runs:
perf record -a -g -F 11 -o perf.data
# Samples at 11Hz frequency using 'cycles' PMU event as a timer
```

**Potential Enhancement (Event-based Sampling):**
```bash
# What could be added for PMU event profiling:
perf record -a -g -e cache-misses -c 1000 -o cache-misses.data
perf record -a -g -e stalled-cycles-frontend -c 1000 -o stalls.data
# Samples every N events (e.g., every 1000 cache misses)
```

**Key Differences:**

| **Current (Time-based)** | **Requested (Event-based)** |
|--------------------------|------------------------------|
| Sample every 1/11th second | Sample every N events |
| Uses `cycles` as timer | Uses specific PMU events |
| Shows "what's running when" | Shows "what's running when events occur" |
| Single flamegraph output | Multiple flamegraphs per event type |
| Available now | Would require implementation |

**Use Cases for Event-based Profiling:**
- **Cache-miss flamegraphs**: Identify memory-bound code paths
- **Stall flamegraphs**: Find CPU pipeline inefficiencies  
- **Branch-miss flamegraphs**: Locate unpredictable code branches
- **Performance debugging**: Separate CPU-bound vs memory-bound bottlenecks

### PMU Events Across Architectures

The UI, the heartbeat and the profile metadata all use generic perf event names (`cycles`, `cache-misses`, ...).
On startup the agent runs `perf list pmu` and resolves each generic event to the name `perf record -e` should use on
that host (`detect_pmu_event_translations` in `gprofiler/profilers/perf_events.py`):

- **x86_64 (Intel, AMD)**: the core PMU lists the generic names, which are used as is.
- **aarch64 (`armv8_pmuv3`, e.g. AWS Graviton)**: the PMU only lists Arm event names, so the generic names are
  translated using `ARCH_PMU_EVENT_TRANSLATIONS`:

| Generic event | aarch64 event | Counts |
|---------------|---------------|--------|
| `cycles` | `cpu_cycles` | CPU cycles |
| `instructions` | `inst_retired` | Instructions architecturally executed |
| `cache-references` | `ll_cache_rd` (or `l1d_cache`) | Last-level cache reads |
| `cache-misses` | `ll_cache_miss_rd` (or `l1d_cache_refill`) | Last-level cache read misses |
| `branch-instructions` | `br_retired` (or `pc_write_retired`) | Branches architecturally executed |
| `branch-misses` | `br_mis_pred` (or `br_mis_pred_retired`) | Mispredicted branches |
| `stalled-cycles-frontend` | `stall_frontend` | Cycles with no operation issued due to the frontend |
| `stalled-cycles-backend` | `stall_backend` | Cycles with no operation issued due to the backend |

Except for the cache events, the translations follow the kernel's own generic event mapping
(`drivers/perf/arm_pmuv3.c`), so the counts are the same as those of the generic events. For the cache events the
kernel uses L1 data cache events, which count about one miss per load whether the data comes from L2 or from memory.
The last-level cache events only count the loads that go to memory, like `cache-misses` on Intel, and are what the
kernel uses for `LLC-loads` / `LLC-load-misses` on aarch64. The core's L3 events (`l3d_cache_*`) aren't used because
they read 0 on Graviton3, whose L3 is the system-level cache outside the core. On CPUs that don't implement the
last-level cache events, the L1 data cache events are used.

Things to keep in mind when comparing hosts:

- `cache-misses` doesn't count the same misses everywhere: last-level cache misses on Intel and aarch64 (reads only on
  aarch64), L2 misses on AMD Zen.
- Not every CPU supports every event, e.g. `stalled-cycles-backend` isn't available on AMD Zen 4.
- PMUs have few programmable counters (6 on Graviton3). Selecting more events than counters makes perf multiplex
  them, so each event is only counted for part of the time.

#### Broader PMU event support

Only the generic events above can be selected. Supporting any PMU event a host reports (e.g. `ll_cache_miss_rd` or
`dtlb_walk` on aarch64, or vendor-specific events on Intel and AMD) requires a bigger refactor:

- **Agent**: report the host's full PMU event inventory (event, PMU, description, CPU model) instead of the fixed
  list, and accept those events in `validate_and_normalize_events`, which currently drops anything outside
  `SUPPORTED_PMU_EVENTS`.
- **Backend**: a table consolidating the PMU events reported per host / CPU model, replacing the
  `supported_perf_events` array, and used to validate profiling requests.
- **UI**: an event picker driven by that table, listing the events supported by the selected hosts with their
  descriptions, and handling selections spanning several architectures.

## 🤔 Why These Profilers Over Alternatives?

gProfiler chose specific profilers for production readiness and performance over popular alternatives:

### Java: async-profiler vs JProfiler/YourKit

**Why async-profiler:**
- **Production Safe**: Uses JVMTI, no code injection or JVM modification
- **Zero Overhead**: Runs inside JVM process, no external process overhead
- **JIT Accuracy**: Handles HotSpot JIT compilation correctly (_[j], _[i] suffixes)
- **Open Source**: No licensing costs, full source code access
- **Battle-tested**: Used by major companies for production profiling

**JProfiler/YourKit limitations:**
- **High Overhead**: GUI-based tools not designed for continuous profiling
- **Licensing Costs**: Commercial tools with per-seat/per-server licensing
- **Resource Usage**: Heavy memory/CPU footprint unsuitable for production
- **Deployment Complexity**: GUI tools hard to automate and deploy at scale
- **JVM Destabilization**: Can cause JVM crashes in production environments

### Python: py-spy/PyPerf vs cProfiler/profile

**Why py-spy/PyPerf:**
- **External Profiling**: No code modification or import statements needed
- **Production Safe**: Minimal process interruption (~100μs for py-spy)
- **Multi-Version Support**: Handles Python 2.7-3.12 automatically
- **True Sampling**: Statistical profiling, not instrumentation-based
- **eBPF Option**: PyPerf uses kernel-level profiling for even lower overhead

**cProfiler/profile limitations:**
- **Code Modification**: Requires importing cProfile and instrumenting code
- **Significant Overhead**: 10-100x performance impact during profiling
- **Deployment Issues**: Need to modify application code and redeploy
- **Limited Scope**: Only profiles instrumented code paths
- **Production Unsafe**: Cannot be left running continuously

### Ruby: rbspy vs ruby-prof/stackprof

**Why rbspy:**
- **External Profiling**: No code changes or gem installation required
- **Production Ready**: Minimal overhead, safe for production use
- **Sampling Profiler**: True statistical profiling vs instrumentation
- **Easy Deployment**: Works with existing Ruby applications out-of-the-box

**ruby-prof/stackprof limitations:**
- **Code Instrumentation**: Requires adding gems and modifying application
- **Performance Impact**: Significant overhead during profiling
- **Production Deployment**: Hard to deploy safely in production environments
- **Version Dependencies**: Gem compatibility issues across Ruby versions

### .NET: dotnet-trace vs JetBrains dotMemory/dotTrace

**Why dotnet-trace:**
- **Official Microsoft Tool**: Supported and maintained by Microsoft
- **EventPipe Integration**: Uses .NET's built-in profiling API
- **Production Optimized**: Designed specifically for production profiling
- **Cross-Platform**: Works on Linux containers and Windows
- **Low Overhead**: Built into .NET runtime, minimal performance impact

**JetBrains tools limitations:**
- **Desktop-Focused**: Designed for development, not server profiling
- **Commercial Licensing**: Expensive per-developer licensing model
- **High Overhead**: Not suitable for continuous production profiling
- **Windows-Centric**: Limited Linux support, especially in containers
- **Deployment Complexity**: GUI tools difficult to automate

### PHP: phpspy vs Xdebug/Blackfire

**Why phpspy:**
- **External Profiling**: No PHP extension installation required
- **Production Safe**: Minimal impact on PHP-FPM processes
- **Zend Engine Access**: Direct memory access for accurate profiling
- **No Code Changes**: Works with existing PHP applications

**Xdebug/Blackfire limitations:**
- **Extension Required**: Must install and configure PHP extensions
- **High Overhead**: Significant performance impact (5-10x slowdown)
- **Production Unsafe**: Xdebug should never run in production
- **Configuration Complexity**: Requires PHP.ini changes and restarts

### System: perf vs Intel VTune/Instruments

**Why perf:**
- **Kernel Integration**: Built into Linux kernel, always available
- **System-wide Profiling**: Profiles entire system, any language/runtime
- **Hardware Sampling**: Uses CPU Performance Monitoring Units (PMU)
- **Universal Coverage**: Works with Go, Node.js, C/C++, Rust, etc.
- **Zero Cost**: Open source, no licensing fees

**Intel VTune/Instruments limitations:**
- **Platform Specific**: VTune requires Intel CPUs, Instruments only macOS
- **Commercial Licensing**: Expensive enterprise licensing
- **Deployment Complexity**: Complex setup for production environments
- **Overkill**: Too feature-heavy for continuous profiling needs
- **Vendor Lock-in**: Tied to specific hardware/OS platforms

## 🎯 Key Architecture Benefits

### Production-First Design
- All profilers chosen specifically for minimal production overhead
- No code modification required across any supported language
- Suitable for 24/7 continuous profiling in high-load environments
- Battle-tested by major companies in production

### Cost Efficiency
- All profilers are open source with no licensing costs
- No per-seat, per-server, or per-CPU licensing models
- Scales horizontally without additional cost
- Reduces total cost of ownership for observability

### Operational Simplicity
- Single agent handles multiple languages automatically
- Automatic process discovery and language detection
- Unified output format across all profilers
- Easy deployment via container or system packages

### Accuracy & Coverage
- Language-specific profilers understand runtime semantics
- System profiler catches everything else (native code, kernel)
- Stack trace merging provides complete application picture
- Hardware-level sampling ensures accurate timing data

This architecture enables gProfiler to provide enterprise-grade continuous profiling across multiple languages while maintaining production safety, cost efficiency, and operational simplicity.

## 🔍 Language Identification: Agent vs Backend

**Key Question**: Are perf stack traces identified as Go/C++/Node.js at the agent side (gProfiler) or in the backend (Performance Studio)?

**Answer**: Language identification happens at **BOTH** levels, but for different purposes:

### Agent Side (gProfiler) - Process-Level Identification

The gProfiler agent identifies **processes** (not individual stack frames) for:

1. **Process Discovery & Profiler Selection**:
```python
# In gprofiler/profilers/perf.py
from granulate_utils.golang import is_golang_process
from granulate_utils.node import is_node_process

# Go process detection via ELF build ID
def is_golang_process(process: Process) -> bool:
    return not is_kernel_thread(process) and get_golang_buildid(process) is not None

# Node.js process detection via executable name
def is_node_process(process: Process) -> bool:
    return not is_kernel_thread(process) and is_process_basename_matching(process, r"^node$")
```

2. **Metadata Collection**:
```python
# Collect Go/Node.js specific metadata
self._metadata_collectors: List[PerfMetadata] = [
    GolangPerfMetadata(self._profiler_state.stop_event),    # Go version, build info
    NodePerfMetadata(self._profiler_state.stop_event),      # Node.js version
]
```

3. **Special Handling**:
   - **Go processes**: No special runtime profiler, relies on perf + symbol resolution
   - **Node.js processes**: Optional perf-maps generation or jitdump injection
   - **C/C++ processes**: Pure perf profiling, no special handling

### Backend Side (Performance Studio) - Frame-Level Identification

The Performance Studio backend identifies **individual stack frames** for visualization:

```go
// In gprofiler-performance-studio/src/gprofiler_flamedb_rest/db/lang_ident.go
var RuntimesRegexps = []LanguageRegexp{
    // Runtime profilers (with suffixes added by agent)
    {Lang: Java, Regexp: regexp.MustCompile("_\\[j]$")},           // async-profiler
    {Lang: Python, Regexp: regexp.MustCompile("_\\[p]$")},        // py-spy/PyPerf
    {Lang: Ruby, Regexp: regexp.MustCompile("_\\[rb]$")},         // rbspy
    {Lang: Net, Regexp: regexp.MustCompile("_\\[net]$")},         // dotnet-trace
    {Lang: PHP, Regexp: regexp.MustCompile("_\\[php]$")},         // phpspy
    {Lang: Kernel, Regexp: regexp.MustCompile("_\\[k]$")},        // perf kernel
    
    // Pattern-based detection (no suffixes, from perf)
    {Lang: NodeJS, Regexp: regexp.MustCompile("^LazyCompile|^InterpretedFunction")}, // V8 JIT
    {Lang: Cpp, Regexp: regexp.MustCompile("::")},                                   // C++ namespaces
    {Lang: Go, Regexp: regexp.MustCompile("\\w\\.(?:\\w|\\()"),                     // Go packages
    {Lang: Other, Regexp: regexp.MustCompile("\\[.+\\.so]")},                       // Shared libraries
}
```

### The Flow: Agent → Backend

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    Language Identification Flow                                │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  1. Agent Side (Process Discovery)                                              │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                                                                         │   │
│  │  • Scans /proc/*/exe for Go build IDs                                   │   │
│  │  • Scans process names for "node" executables                           │   │
│  │  • Scans /proc/*/maps for runtime libraries                             │   │
│  │                                                                         │   │
│  │  Decision: Which profiler to use per process                            │   │
│  │  ├─ Java process → async-profiler                                       │   │
│  │  ├─ Python process → py-spy/PyPerf                                      │   │
│  │  ├─ Go process → perf only (no runtime profiler)                        │   │
│  │  ├─ Node.js process → perf + optional maps                              │   │
│  │  └─ C/C++ process → perf only                                           │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  2. Stack Trace Collection                                                      │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                                                                         │   │
│  │  Runtime Profilers add suffixes:                                        │   │
│  │  • async-profiler → com.app.Handler.process_[j]                         │   │
│  │  • py-spy → main.py:main_[p]                                            │   │
│  │  • rbspy → User#authenticate_[rb]                                       │   │
│  │                                                                         │   │
│  │  System Profiler (perf) produces raw symbols:                           │   │
│  │  • Go → main.main, runtime.mallocgc                                     │   │
│  │  • Node.js → LazyCompile:*handleRequest                                 │   │
│  │  • C++ → std::vector<int>::push_back                                    │   │
│  │  • Kernel → __sys_read_[k] (suffix added by agent)                      │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  3. Backend Side (Frame Classification)                                         │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                                                                         │   │
│  │  Pattern matching on individual frames:                                 │   │
│  │                                                                         │   │
│  │  Suffix-based (from runtime profilers):                                 │   │
│  │  • "_[j]" → Java                                                        │   │
│  │  • "_[p]" → Python                                                      │   │
│  │  • "_[rb]" → Ruby                                                       │   │
│  │                                                                         │   │
│  │  Pattern-based (from perf):                                             │   │
│  │  • "::" → C++                                                           │   │
│  │  • "LazyCompile:" → Node.js                                             │   │
│  │  • "package.function" → Go (with context resolution)                    │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Why This Two-Level Approach?

1. **Agent Side (Process-Level)**:
   - **Purpose**: Decide which profiler to run for each process
   - **Speed**: Fast process scanning using shell commands
   - **Accuracy**: High reliability for process identification
   - **Scope**: Process-wide decisions

2. **Backend Side (Frame-Level)**:
   - **Purpose**: Categorize individual stack frames for visualization
   - **Granularity**: Per-frame language classification
   - **Context**: Can analyze full stack context for ambiguous frames
   - **Flexibility**: Easy to update patterns without agent changes

### Special Case: Go Language Detection

Go has **two-stage detection** in the backend:

```go
// Go detection with context resolution
{Lang: Go, Regexp: regexp.MustCompile("\\w\\.(?:\\w|\\()"), 
 ControlRegexp: regexp.MustCompile("^runtime(\\.|/).+")}

func identFrameLangAndSpecialType(frameName string) (string, string) {
    if runtime.Lang == Go {
        if runtime.ControlRegexp.MatchString(frameName) {  // "runtime.mallocgc"
            return "Go", ""           // Definite Go runtime
        } else {
            return "MayBeGo", ""      // Maybe Go (needs parent context)
        }
    }
}
```

**Context Resolution**: `MayBeGo` frames are resolved by checking parent frames:
- If parent is `Go`, child becomes `Go`
- If parent is not `Go`, child becomes `Other`

### Summary

- **Agent**: Identifies **processes** to choose the right profiler
- **Backend**: Identifies **stack frames** for language-specific visualization
- **Go/Node.js/C++**: Identified primarily in backend via pattern matching
- **Java/Python/Ruby**: Identified by agent (via process detection) + backend (via suffixes)
- **Result**: Accurate language attribution at both process and frame levels

### Profiler Architecture: Runtime vs System

gProfiler uses two types of profilers that work together:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        Profiler Architecture                                   │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│ ┌─────────────────────────────────┐  ┌───────────────────────────────────────┐ │
│ │        Runtime Profilers        │  │         System Profiler              │ │
│ │    (Process-Specific)           │  │       (System-Wide)                  │ │
│ ├─────────────────────────────────┤  ├───────────────────────────────────────┤ │
│ │                                 │  │                                       │ │
│ │ ┌─────────────────────────────┐ │  │ ┌───────────────────────────────────┐ │ │
│ │ │        Java                 │ │  │ │          perf                     │ │ │
│ │ │   async-profiler            │ │  │ │    (Linux system profiler)       │ │ │
│ │ │   • JVM-specific            │ │  │ │                                   │ │ │
│ │ │   • Attaches to Java PIDs   │ │  │ │ ┌─────────────────────────────────┐ │ │
│ │ │   • Java stack traces       │ │  │ │ │  Profiles Everything:           │ │ │
│ │ └─────────────────────────────┘ │  │ │ │  • Go applications             │ │ │
│ │                                 │  │ │ │  • Node.js (with maps)         │ │ │
│ │ ┌─────────────────────────────┐ │  │ │ │  • C/C++/Rust                  │ │ │
│ │ │        Python               │ │  │ │ │  • Native libraries            │ │ │
│ │ │   PyPerf (eBPF) OR py-spy   │ │  │ │ │  • Kernel functions            │ │ │
│ │ │   • Python-specific         │ │  │ │ └─────────────────────────────────┘ │ │
│ │ │   • Python stack traces     │ │  │ │                                   │ │ │
│ │ └─────────────────────────────┘ │  │ │ Uses: perf record -a -g           │ │ │
│ │                                 │  │ │ • System-wide profiling           │ │ │
│ │ ┌─────────────────────────────┐ │  │ │ • Cannot cherry-pick processes    │ │ │
│ │ │        Ruby                 │ │  │ │ • Captures all CPU activity       │ │ │
│ │ │   rbspy                     │ │  │ └───────────────────────────────────┘ │ │
│ │ │   • Ruby-specific           │ │  │                                       │ │
│ │ │   • Ruby stack traces       │ │  └───────────────────────────────────────┘ │
│ │ └─────────────────────────────┘ │                                          │
│ │                                 │                                          │
│ │ ┌─────────────────────────────┐ │                                          │
│ │ │        .NET                 │ │                                          │
│ │ │   dotnet-trace              │ │                                          │
│ │ │   • .NET-specific           │ │                                          │
│ │ │   • .NET stack traces       │ │                                          │
│ │ └─────────────────────────────┘ │                                          │
│ │                                 │                                          │
│ │ ┌─────────────────────────────┐ │                                          │
│ │ │        PHP                  │ │                                          │
│ │ │   phpspy                    │ │                                          │
│ │ │   • PHP-specific            │ │                                          │
│ │ │   • PHP stack traces        │ │                                          │
│ │ └─────────────────────────────┘ │                                          │
│ └─────────────────────────────────┘                                          │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Stack Trace Merging & Scaling

gProfiler merges stack traces from different profilers using a scaling algorithm:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          Stack Trace Merging Process                           │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  Input: Runtime Profiler Data + System Profiler Data                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                                                                         │   │
│  │  Runtime Profilers          System Profiler (perf)                     │   │
│  │  ┌─────────────────┐        ┌─────────────────────────────────────┐     │   │
│  │  │ PID 1234 (Java) │        │ PID 1234: 100 samples               │     │   │
│  │  │ 50 samples      │   VS   │ PID 5678: 80 samples                │     │   │
│  │  │                 │        │ PID 7777: 60 samples (Go)           │     │   │
│  │  │ PID 5678 (Py)   │        │ PID 9999: 40 samples (Node.js)      │     │   │
│  │  │ 30 samples      │        └─────────────────────────────────────┘     │   │
│  │  └─────────────────┘                                                    │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  Merging Algorithm:                                                             │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                                                                         │   │
│  │  For each PID:                                                          │   │
│  │    if runtime_profiler_data[pid] exists:                               │   │
│  │      # Scale runtime data to match perf sample count                   │   │
│  │      ratio = perf_samples / runtime_samples                            │   │
│  │      scaled_stacks = scale_sample_counts(runtime_stacks, ratio)        │   │
│  │      final_data[pid] = scaled_stacks  # Use runtime profiler data      │   │
│  │    else:                                                                │   │
│  │      final_data[pid] = perf_data[pid]  # Use system profiler data      │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Output Format

gProfiler produces collapsed stack trace files with metadata and enrichment:

```
Format: [app_metadata_idx];[container];[process];[appid];[stack] count

Examples:
0;myapp-container;java;appid: spring-boot;com.app.Handler.process 30
0;python-worker;python;appid: django;main.py:main;handler.py:process 16  
0;;go-service;;main.main;handler.processRequest;runtime.mallocgc 10
0;node-api;node;;server.js:handleRequest;database.js:query 8

Stack Frame Suffixes (identify profiler source):
• _[p]   - Python (py-spy/PyPerf)
• _[rb]  - Ruby (rbspy)  
• _[php] - PHP (phpspy)
• _[net] - .NET (dotnet-trace)
• _[k]   - Kernel (perf)
• (none) - Native/Java/Go (perf/async-profiler)

### Backend Language Detection

The Performance Studio backend distinguishes between different languages from perf stack traces using pattern matching:

```go
// Language identification patterns in Performance Studio
var RuntimesRegexps = []LanguageRegexp{
    // Java variants
    {Lang: Java, Regexp: regexp.MustCompile("_\\[j]$")},           // Java JIT
    {Lang: Java, Regexp: regexp.MustCompile("_\\[i]$")},           // Java inline
    {Lang: Java, Regexp: regexp.MustCompile("_\\[1]$")},           // Java C1 compiler
    {Lang: Java, Regexp: regexp.MustCompile("_\\[0]$")},           // Java interpreted
    
    // Runtime profilers (with suffixes)
    {Lang: Python, Regexp: regexp.MustCompile("_\\[p]$|_\\[pe]$")},  // py-spy/PyPerf
    {Lang: Python, Regexp: regexp.MustCompile("\\.py\\)$")},          // Python file extensions
    {Lang: PHP, Regexp: regexp.MustCompile("_\\[php]$")},             // phpspy
    {Lang: Ruby, Regexp: regexp.MustCompile("_\\[rb]$")},             // rbspy
    {Lang: Net, Regexp: regexp.MustCompile("_\\[net]$")},             // dotnet-trace
    {Lang: Kernel, Regexp: regexp.MustCompile("_\\[k]$")},            // Kernel functions
    
    // Native languages (pattern-based detection)
    {Lang: NodeJS, Regexp: regexp.MustCompile("^LazyCompile|^InterpretedFunction")}, // V8 patterns
    {Lang: Cpp, Regexp: regexp.MustCompile("::")},                    // C++ namespace separator
    {Lang: Go, Regexp: regexp.MustCompile("\\w\\.(?:\\w|\\()")},      // Go package.function
    
    // Other patterns
    {Lang: Other, Regexp: regexp.MustCompile("_\\[pn]$")},            // Native Python extensions
    {Lang: Other, Regexp: regexp.MustCompile("\\[.+\\.so]")},         // Shared libraries
}
```

**Language Detection Logic:**

1. **Suffix-based Detection** (Runtime Profilers):
   - `_[p]` → Python (py-spy/PyPerf)
   - `_[j]` → Java (async-profiler)
   - `_[k]` → Kernel (perf)
   - `_[rb]` → Ruby (rbspy)

2. **Pattern-based Detection** (System Profiler):
   - `::` → C++ (namespace separator)
   - `LazyCompile:` → Node.js (V8 JIT compilation)
   - `package.function` → Go (package notation)
   - `runtime.` → Go runtime functions

3. **Special Go Detection**:
   ```go
   // Go has two-stage detection
   if runtime.Lang == Go {
       if runtime.ControlRegexp.MatchString(frameName) { // "^runtime(\\.|/).+"
           return "Go", ""           // Definite Go runtime
       } else {
           return "Go?", ""          // Maybe Go (needs parent context)
       }
   }
   ```

4. **Context-aware Classification**:
   - `Go?` frames are resolved by checking parent frames
   - If parent is `Go`, child becomes `Go`
   - If parent is not `Go`, child becomes `Other`

**Example Stack Trace Classifications:**

```
// Java (async-profiler with suffixes)
com.app.Handler.process_[j]                    → Java (JIT compiled)
com.app.Service.call_[i]                       → Java (inlined)
com.app.DB.query_[0]                           → Java (interpreted)

// Python (py-spy/PyPerf with suffixes)  
main.py:main_[p]                               → Python
handler.py:process_request_[p]                 → Python
numpy.core._multiarray_umath.so_[pn]           → Other (native extension)

// Go (pattern-based, no suffixes)
main.main                                      → Go? (needs context)
runtime.mallocgc                               → Go (definite runtime)
net/http.(*Server).Serve                       → Go? → Go (if parent is Go)

// Node.js (V8 patterns, no suffixes)
LazyCompile:*handleRequest /app/server.js:42   → Node.js
InterpretedFunction:processData /app/api.js:15 → Node.js

// C++ (namespace patterns, no suffixes)
std::vector<int>::push_back                    → C++
boost::asio::io_context::run                   → C++

// Kernel (perf with suffix)
__sys_read_[k]                                 → Kernel
do_syscall_64_[k]                              → Kernel

// Shared libraries (pattern-based)
[libc.so.6]                                    → Other
[libssl.so.1.1]                                → Other
```

## Theory of operation
gProfiler invokes `perf` in system wide mode, collecting profiling data for all running processes.
Alongside `perf`, gProfiler invokes runtime-specific profilers for processes based on these environments:
* Java runtimes (version 7+) based on the HotSpot JVM, including the Oracle JDK and other builds of OpenJDK like AdoptOpenJDK and Azul Zulu.
  * Uses async-profiler.
* The CPython interpreter, versions 2.7 and 3.5-3.10.
  * eBPF profiling (based on PyPerf) requires Linux 4.14 or higher; see [Python profiling options](#python-profiling-options) for more info.
  * If eBPF is not available for whatever reason, py-spy is used.
* PHP (Zend Engine), versions 7.0-8.0.
  * Uses [Granulate's fork](https://github.com/Granulate/phpspy/) of the phpspy project.
* Ruby versions (versions 1.9.1 to 3.0.1)
  * Uses [Granulate's fork](https://github.com/Granulate/rbspy) of the [rbspy](https://github.com/rbspy/rbspy) profiler.
* NodeJS (version >= 10 for functioning `--perf-prof`):
  * Uses `perf inject --jit` and NodeJS's ability to generate jitdump files. See [NodeJS profiling options](#nodejs-profiling-options).
  * Can also generate perf maps at runtime - see [attach-maps](#attach-maps) option.
* .NET runtime
  * Uses dotnet-trace.

The runtime-specific profilers produce stack traces that include runtime information (i.e, stacks of Java/Python functions), unlike `perf` which produces native stacks of the JVM / CPython interpreter.
The runtime stacks are then merged into the data collected by `perf`, substituting the *native* stacks `perf` has collected for those processes.

## Architecture support

| Runtime                    | x86_64                            | Aarch64                           |
|----------------------------|-----------------------------------|-----------------------------------|
| perf (native, Golang, ...) | :heavy_check_mark:                | :heavy_check_mark:                |
| Java (async-profiler)      | :heavy_check_mark:                | :heavy_check_mark:                |
| Python (py-spy)            | :heavy_check_mark:                | :heavy_check_mark:                |
| Python (PyPerf eBPF)       | :heavy_check_mark:                | :x:                               |
| Ruby (rbspy)               | :heavy_check_mark:                | :heavy_check_mark:                |
| PHP (phpspy)               | :heavy_check_mark:                | :heavy_check_mark: (experimental) |
| NodeJS (perf)              | :heavy_check_mark:                | :heavy_check_mark:                |
| .NET (dotnet-trace)        | :heavy_check_mark: (experimental) | :heavy_check_mark: (experimental) |

## perf-less mode

It is possible to run gProfiler without using `perf` - this is useful where `perf` can't be used, for whatever reason (e.g permissions). This mode is enabled by `--perf-mode disabled`.

In this mode, gProfiler uses runtime-specific profilers only, and their results are concatenated (instead of scaled into the results collected by `perf`). This means that, although the results from different profilers are viewed on the same graph, they are not necessarily of the same scale: so you can compare the samples count of Java to Java, but not Java to Python.

## Data format

This section describes the data format output by the profiler. Some of it is relevant to the local output files (`.col`) files and some of it is relevant both for the local output files and for the data as viewable in the Performance Studio.

### Collapsed files

The collapsed file (`.col`) is a [collapsed/folded stacks file](https://github.com/brendangregg/FlameGraph#2-fold-stacks) that'll be written locally per profiling session if gProfiler was invoked with the `-o` switch.  
The file begins with a "comment line", starting with `#`, which contains a JSON of metadata about the profile. Following lines are *stacks* - they consist of *frames* separated by `;`, with the ending of each line being a space followed by a number - how many *samples* were collected with this stack.  
The first frame of each stack is an index in the application metadata array (which is part of the aforementioned JSON), for the process recorded in this sample.  
The second frame is the container name that the process recorded in this sample runs in; if the process is not running in a container, this frame will be empty.  
The third frame is the process name - essentially the process `comm` in Linux.  
All following frames are the output of the profiler which emitted the sample (usually - function names). Frames are described in [frame format](#frame-format).

### Application identifiers

An application identifier ("appid" for short) is an optional frame that follows the process name frame. This frame has the format `appid: ...`. Per profiled process, gProfiler attempts to extract its appid, and "inject" it into the profile collected for that process - the purpose is to give the user more context about the source application of the proceeding frames.  
For example, a Python application invoked as `cd /path/to/my/app && python myscript.py` will have the following appid: `appid: python: myscript.py (/path/to/my/app/myscript.py)` - this appid tells us it's a Python application running `myscript.py` and gives in parenthesis the absolute path of the executed script.

gProfiler currently supports appids for Java, Python, NodeJS & Ruby, with each runtime having possibly more than one implementation (e.g in Python, the appid of a Gunicorn-based application is decided differently, because the app doesn't specify a "Python script" to invoke, but instead specifies a WSGI application spec). You can see the various implementations in [application_identifiers.py](./gprofiler/metadata/application_identifiers.py)

Collection of appids is enabled by default and can be disabled with the `--disable-application-identifiers` switch.

### Frame format

Each frame represents a function as identified by gProfiler. Since gProfiler aggregated frames collected by different profilers, the frame format differs depending on which profiler collected it, and from which runtime it originates (e.g Python vs Java).
Additionally, each frame has a suffix which designates the profiler it originated from and the logical "source" for the frame - is it Java code? Kernel code? Native library code?

| Runtime                               | Frame Format                                                                                                                                                                            | Suffix                                                                                                   |
|---------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------|
| Native - C, C++, Rust (perf)          | Symbol name                                                                                                                                                                             | None                                                                                                     |
| Golang (perf)                         | Symbol name                                                                                                                                                                             | None                                                                                                     |
| Java (async-profiler)                 | Method FQN + signature, per [async-profiler's `-g` switch](https://github.com/jvm-profiling-tools/async-profiler#profiler-options). `;`s in the method signature are replaced with `\|` | [Per asnyc-profiler `-a` switch](https://github.com/jvm-profiling-tools/async-profiler#profiler-options) |
| Native (async-profiler)               | Symbol name                                                                                                                                                                             | None                                                                                                     |
| Python (PyPerf)                       | `package.(instance class if it's a method/classmethod).function_name (filename.py:line_number)`                                                                                         | `_[p]`                                                                                                   |
| Native (PyPerf)                       | Symbol name                                                                                                                                                                             | `_[pn]`                                                                                                  |
| Python (py-spy)                       | `package.function_name (filename.py:line_number)`                                                                                                                                       | `_[p]`                                                                                                   |
| NodeJS (perf)                         | Per NodeJS                                                                                                                                                                              | None                                                                                                     |
| Ruby (rbspy)                          | Per rbspy                                                                                                                                                                               | `_[rb]`                                                                                                  |
| PHP (phpspy)                          | Per phpspy                                                                                                                                                                              | `_[php]`                                                                                                 |
| .NET (dotnet-trace)                   | Per dotnet-trace                                                                                                                                                                        | `_[net]`                                                                                                 |
| Kernel (perf, async-profiler, PyPerf) | Function name                                                                                                                                                                           | `_[k]`                                                                                                   |

# Security Concerns

Consider reviewing Docker security docs described in https://docs.docker.com/engine/security/
If you're using Docker, it's recommended to enable the following security settings if required by your organization and applicable:
* Enable AppArmor Profile (https://docs.docker.com/engine/security/apparmor/)
* Enable SELinux 
* Update the IP address bindings in the docker-compose file (they default to 0.0.0.0 on all interfaces) to your specific hosts.  
  * Default port settings in docker-compose are: "8080:80" and "4433:443"
  * You can bind them to your desired ip by setting them to: "{my_ip}:8080:80" and "{my_ip}:4433:443"
* Mount the container's root file system as read only
* Restrict the container from acquiring additional privileges using --no-new-privilege
* Make sure the Docker commands always make use of the latest version of their images

Be aware that when deployed as a container...

* gProfiler may require privileged mode to operate.  Make sure you understand the security implications by reviewing the [Docker security docs](https://docs.docker.com/engine/security/) 
* The host PID and user namespace will be shared with gProfiler to allow it to run as root outside the container.  Consider using [rootless mode](#rootless-mode)


# Building

Please refer to the [building](./CONTRIBUTING.md#building) section.

# Contribute
We welcome all feedback and suggestion through Github Issues:
* [Submit bugs and feature requests](https://github.com/intel/gprofiler/issues)
* Upvote [popular feature requests](https://github.com/intel/gprofiler/issues?q=is%3Aopen+is%3Aissue+label%3Aenhancement+sort%3Areactions-%2B1-desc+)

## Releasing a new version
1. Update `__version__` in `__init__.py`.
2. Create a tag with the same version (after merging the `__version__` update) and push it.

We recommend going through our [contribution guide](./CONTRIBUTING.md) for more details.

# Credits
* [async-profiler](https://github.com/jvm-profiling-tools/async-profiler) by [Andrei Pangin](https://github.com/apangin). See [our fork](https://github.com/Granulate/async-profiler).
* [py-spy](https://github.com/benfred/py-spy) by [Ben Frederickson](https://github.com/benfred). See [our fork](https://github.com/Granulate/py-spy).
* [bcc](https://github.com/iovisor/bcc) (for PyPerf) by the IO Visor project. See [our fork](https://github.com/Granulate/bcc).
* [phpspy](https://github.com/adsr/phpspy) by [Adam Saponara](https://github.com/adsr). See [our fork](https://github.com/Granulate/phpspy).
* [rbspy](https://github.com/rbspy/rbspy) by the rbspy project. See [our fork](https://github.com/Granulate/rbspy).
* [dotnet-trace](https://github.com/dotnet/diagnostics/tree/main/src/Tools/dotnet-trace)
* [Datadog Agent Integrations](https://github.com/DataDog/integrations-core) for the Spark, YARN metrics

# Footnotes

<a name="perf-native">1</a>: To profile native programs that were compiled without frame pointers, make sure you use the `--perf-mode smart` (which is the default). Read more about it in the [Profiling options](#profiling-options) section[↩](#a1)
