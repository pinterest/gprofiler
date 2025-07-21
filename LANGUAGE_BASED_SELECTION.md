# Language-Specific Profiling Configuration Guide

This guide covers how to enable frame pointers and DWARF debug information for different programming languages, with specific recommendations based on CPU usage patterns.

## How to Ensure Sufficient Symbol Resolution in Stack Traces

### **What Are Symbols?**

**Symbols** are metadata that map memory addresses to human-readable names in your program. They include:

- **Function names**: `main()`, `calculate_sum()`, `http_handler()`
- **Variable names**: Global and static variable identifiers
- **Source locations**: File names and line numbers
- **Type information**: Class names, struct definitions

**Example of symbolized vs unsymbolized stack trace:**
```
# With symbols (good)
main() -> calculate_tax() -> parse_amount() -> strlen()

# Without symbols (bad)  
0x40123a -> 0x401156 -> 0x7fff8b2c1234 -> [unknown]
```

**Symbol Storage**: Symbols are stored in different sections of binaries:
- **Symbol table** (`.symtab`): Function and variable names
- **DWARF debug info** (`.debug_*`): Rich debugging information with line numbers
- **Dynamic symbols** (`.dynsym`): Exported symbols for shared libraries

### **Diagnosing Insufficient Symbolization**

If your flamegraphs show shallow call stacks with missing function names or excessive `[unknown]` frames, this indicates insufficient symbol resolution. Common symptoms include:

- **Truncated call stacks**: Only 1-3 frames visible instead of deep call chains
- **Missing function names**: Stack traces show memory addresses (`0x7fff...`) instead of function names
- **High `[unknown]` frame ratio**: >30% of samples contain unresolved symbols

### **Root Cause Analysis**

**Frame Pointer Absence**: Most production binaries are compiled with frame pointer omission (`-fomit-frame-pointer`) for performance optimization, making stack unwinding impossible via register-based traversal.

**Stripped Debug Symbols**: Release builds typically strip DWARF debug information to reduce binary size, eliminating the metadata required for address-to-symbol resolution.

### **Technical Solutions**

#### **Primary Recommendation: Enable Frame Pointers**
Frame pointers provide hardware-assisted stack unwinding through the `%rbp` register (x86_64) or `r29` (ARM64), offering minimal profiling overhead and reliable call chain reconstruction.

**Performance Cost**: 2-5% CPU overhead due to register pressure (one less general-purpose register available for optimization).

#### **Alternative for CPU-Critical Applications (>95% CPU utilization): DWARF Debug Information**
For applications where frame pointer overhead is prohibitive, compile with DWARF debug symbols. This enables stack unwinding through .debug_frame/.eh_frame sections without runtime performance impact.

**Trade-offs**:
- ✅ **Zero runtime overhead**: No register allocation impact
- ⚠️ **Increased binary size**: 2-10x larger executables
- ⚠️ **Higher profiler memory usage**: DWARF unwinding requires larger buffers (8-16KB per thread)
- ⚠️ **Slower profiling**: Complex debug information parsing increases CPU overhead
- ❌ **Memory fragmentation**: Large debug sections can cause allocation issues
- ❌ **Deployment complexity**: Separate debug symbol management required
- ❌ **Profiler instability**: DWARF unwinding may fail on corrupted stack frames or optimized code
- ❌ **Limited availability**: Debug symbols often stripped in production environments

## Quick Reference Table: Enable Frame Pointers & DWARF by Language

| Language | Enable Frame Pointers | Enable DWARF Debug Info | Notes |
|----------|----------------------|-------------------------|-------|
| **C/C++** | `gcc -fno-omit-frame-pointer` | `gcc -g` | Most critical for optimized builds |
| **Go** | `go build -gcflags="-framepointer=true"` | Default enabled | Usually already enabled |
| **Rust** | `force-frame-pointers = true` in Cargo.toml | `debug = true` in Cargo.toml | Release builds disable both by default |
| **Node.js** | Default enabled (V8) | `npm config set debug true` | For native modules only |
| **Java** | `java -XX:+PreserveFramePointer` | `javac -g` | JVM flags + compilation |
| **Python** | Default enabled (CPython) | `export CFLAGS="-g"` | For C extensions |
| **Ruby** | Default enabled (Ruby VM) | `export CFLAGS="-g"` | For native gems |

### **Compilation Examples by Language**

#### **C/C++ Applications**
```bash
# Frame pointers + DWARF (low CPU services)
gcc -O2 -fno-omit-frame-pointer -g myapp.c -o myapp

# DWARF only (high CPU services)  
gcc -O2 -g myapp.c -o myapp

# Check what's enabled
objdump -h myapp | grep debug    # Check for DWARF sections
readelf -h myapp | grep Machine  # Check architecture
```

#### **Go Applications**
```bash
# Default build (both enabled)
go build myapp.go

# Force enable frame pointers if disabled
go build -gcflags="-framepointer=true" myapp.go

# Enable debug info if stripped
go build -ldflags="" myapp.go  # Don't use -s -w

# Check what's enabled
go tool nm myapp | head          # Check for symbols
file myapp                       # Check if stripped
```

#### **Rust Applications**
```toml
# Cargo.toml for frame pointers + DWARF
[profile.release]
debug = true                     # Enable DWARF
force-frame-pointers = true      # Enable frame pointers
strip = false                    # Don't strip symbols

# For high CPU services (DWARF only)
[profile.release]
debug = true                     # Enable DWARF  
force-frame-pointers = false     # Disable frame pointers
```

#### **Node.js Applications**
```bash
# Default Node.js (frame pointers enabled)
node myapp.js

# Better perf integration
node --perf-prof --perf-prof-unwinding-info myapp.js

# For native modules with debug info
export CFLAGS="-g -fno-omit-frame-pointer"
npm rebuild
```

#### **Java Applications**
```bash
# Compilation with debug info
javac -g MyApp.java

# Runtime with frame pointers
java -XX:+PreserveFramePointer MyApp

# Both together
javac -g MyApp.java && java -XX:+PreserveFramePointer MyApp
```

#### **Python Applications**
```bash
# Default Python (frame pointers enabled)
python myapp.py

# For C extensions with debug info
export CFLAGS="-g -fno-omit-frame-pointer"
pip install --force-reinstall numpy  # example
```

#### **Ruby Applications**
```bash
# Default Ruby (frame pointers enabled)
ruby myapp.rb

# For native gems with debug info
export CFLAGS="-g -fno-omit-frame-pointer"
gem install some-native-gem
```

### **Verification Commands**

#### **Check if Frame Pointers are Enabled**
```bash
# For compiled binaries
objdump -d myapp | grep -E "push.*%rbp|mov.*%rsp,%rbp" | head -5

# For running processes (if available)
perf record -g --call-graph=fp -p PID sleep 1
perf script | head -20
```

#### **Check if DWARF Debug Info is Available**
```bash
# Check for debug sections
objdump -h myapp | grep debug
readelf -S myapp | grep debug

# Check if symbols are stripped
file myapp
nm myapp | head -5  # Should show symbols if not stripped
```

#### **Test Profiling Quality**
```bash
# Test frame pointer profiling
perf record -g --call-graph=fp -p PID sleep 5
perf script | grep -v "unknown" | wc -l

# Test DWARF profiling  
perf record -g --call-graph=dwarf -p PID sleep 5
perf script | grep -v "unknown" | wc -l
```

## Performance Impact Summary

| Language | Frame Pointers Impact | DWARF Impact | Recommendation |
|----------|----------------------|--------------|----------------|
| C/C++    | 2-5% CPU loss        | None         | DWARF for high-CPU services |
| Go       | <1% CPU loss         | None         | Keep defaults (both enabled) |
| Rust     | 1-3% CPU loss        | None         | DWARF for high-CPU services |
| Node.js  | Negligible           | None         | Keep defaults |
| Java     | <1% CPU loss         | None         | Keep defaults |
| Python   | Negligible           | None         | Keep defaults |
| Ruby     | Negligible           | None         | Keep defaults |

---

## Binary Size Impact

| Debug Option | Typical Size Increase | Mitigation |
|--------------|----------------------|------------|
| DWARF Info   | 2-10x larger         | Use `strip` in final deployment, keep debug symbols separately |
| Frame Pointers | ~5% code size      | Negligible in most cases |

---

## Container Deployment Best Practices

### **Multi-stage Docker Build**
```dockerfile
# Build stage with debug info
FROM gcc:latest as builder
COPY . .
RUN gcc -O2 -g -fno-omit-frame-pointer app.c -o app

# Production stage
FROM debian:slim
COPY --from=builder /app /usr/local/bin/
# Keep debug symbols available but separate
COPY --from=builder /app.debug /usr/local/debug/
```

### **Kubernetes ConfigMap for Profiling**
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: profiling-config
data:
  high-cpu-services: "--perf-mode=dwarf"
  low-cpu-services: "--perf-mode=smart"
  debug-services: "--perf-mode=fp --verbose"
```

---

## Troubleshooting Guide

### **Poor Stack Quality**
- **Symptom**: Shallow stacks, many `[unknown]` frames
- **Solution**: Enable DWARF debug info, check if binaries are stripped

### **High Profiler Memory Usage**
- **Symptom**: gProfiler restarts due to memory usage
- **Solution**: Reduce `--perf-dwarf-stack-size`, use `--perf-mode=fp`

### **High CPU Overhead from Profiling**
- **Symptom**: Application performance degrades during profiling
- **Solution**: Use `--perf-mode=fp`, reduce profiling frequency

### **Missing JIT Symbols**
- **Symptom**: Node.js/Java shows only native frames
- **Solution**: Enable `--nodejs-mode=perf` or Java flight recorder integration

---
---

This guide should help you choose the right profiling configuration based on your service's CPU usage patterns and performance requirements.
