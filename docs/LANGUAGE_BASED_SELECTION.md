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

## Deep Dive: How Profiling and Trace Collection Works (C++, Java, Native)

### 1. How a C++ or Java Program Is Loaded and Executed

#### **C++: Step-by-Step Example (`./myapp`)**

**1. Compilation: Source Code → Object Files**
```cpp
// main.cpp
#include <iostream>
int calculate(int x) { return x * 2; }
int main() { std::cout << calculate(5); return 0; }
```
```bash
g++ -c main.cpp -o main.o    # Creates main.o (machine code + symbols)
```
- **What happens**: Compiler translates C++ code into machine instructions
- **Output**: `main.o` contains compiled functions but unresolved library calls

**2. Linking: Object Files + Libraries → Executable**
```bash
g++ main.o -o myapp    # Links with libc, libstdc++, creates ELF binary
```
- **What happens**: Linker combines your code with system libraries (printf, iostream, etc.)
- **Output**: `myapp` executable that can run independently

**3. ELF Binary Structure: What's Inside the Executable**
```bash
readelf -S myapp    # Shows sections inside the binary
```
```
Section Headers:
[Nr] Name          Type     Address   Size
[ 1] .text         PROGBITS 0x401000  0x1234   # Your compiled code
[ 2] .data         PROGBITS 0x402000  0x100    # Global variables
[ 3] .symtab       SYMTAB   0x403000  0x500    # Function names/addresses
[ 4] .debug_info   PROGBITS 0x404000  0x2000   # Debug symbols (if -g flag)
```
- **What it contains**: Machine code, data, symbol table (function names), debug info

**4. Program Loading: OS Maps Binary into Memory**
```bash
./myapp    # OS loader reads ELF and maps it into process memory
```
```
Process Memory Layout:
0x400000: [Program Code]     ← .text section loaded here
0x500000: [Program Data]     ← .data section loaded here  
0x600000: [libc.so]         ← Standard library loaded here
0x700000: [Stack]           ← Function call stack grows down
```
- **What happens**: OS reads ELF headers and copies sections into RAM at specific addresses

**5. Dynamic Linking: Resolving Library Function Calls**
```bash
ldd myapp    # Shows which libraries will be loaded
```
```
libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6
libstdc++.so.6 => /usr/lib/x86_64-linux-gnu/libstdc++.so.6
```
- **What happens**: Dynamic linker (`ld.so`) finds `std::cout`, `printf` in libraries and "patches" your code to call the right memory addresses
- **Example**: Your code calls `std::cout` → linker replaces with actual address `0x600123`

**6. CPU Startup: Setting Up Execution**
```
Initial CPU State:
RIP (Instruction Pointer) = 0x401000    ← Points to _start function
RSP (Stack Pointer)       = 0x7fff000   ← Points to top of stack
RBP (Frame Pointer)       = 0x0         ← Not set yet
```
- **What happens**: OS sets up CPU registers and jumps to your program's entry point
- **Flow**: `_start` → `__libc_start_main` → `main()`

**7. Function Calls: How the Stack Works**
```cpp
main() {           // RSP=0x7fff000, RBP=0x7fff000
  calculate(5);    // Call pushes return address, sets up new frame
}                  // Return pops back to main

int calculate(int x) {  // New stack frame created
  return x * 2;         // Local computation
}                       // Frame destroyed, return to caller
```

**8. Stack Frame Examples: With vs Without Frame Pointers**

Let's trace through this simple program to see how the stack works:

```cpp
// Example program
int calculate(int x) {
    int result = x * 2;    // Local variable
    return result;
}

int main() {
    int value = 5;         // Local variable  
    int answer = calculate(value);
    return 0;
}
```

**With Frame Pointers (`g++ -fno-omit-frame-pointer`):**

When `main()` calls `calculate(5)`, here's what happens to the stack:

```
Memory Address    Content                     CPU Register
0x7fff000:       [value = 5]                ← main's RBP points here
0x7ffeff8:       [answer = ?]               
0x7ffeff0:       [return addr: main+20]     ← Where to return after calculate()
0x7ffefe8:       [saved RBP: 0x7fff000]    ← Link back to main's frame
0x7ffefe0:       [x = 5]                    ← calculate's RBP points here  
0x7ffefdd:       [result = 10]              ← calculate's RSP points here
```

**How profiler unwinds the stack:**
1. Start at current function: `calculate()` (RBP = 0x7ffefe0)
2. Follow saved RBP: Read 0x7ffefe8 → get 0x7fff000 (main's frame)
3. Follow saved RBP: Read 0x7fff008 → get 0x0 (end of stack)
4. **Result**: `calculate() → main() → _start`

**Without Frame Pointers (`g++ -fomit-frame-pointer`, optimized):**

Same function call, but no RBP chain:

```
Memory Address    Content                     CPU Register
0x7fff000:       [value = 5]                
0x7ffeff8:       [answer = ?]               
0x7ffeff0:       [return addr: main+20]     
0x7ffefe8:       [x = 5]                    
0x7ffefe0:       [result = 10]              ← Only RSP points here (no RBP)
```

**How profiler tries to unwind:**
1. Start at current instruction: `calculate()+15`
2. **Problem**: No RBP chain to follow!
3. **Solution**: Must use DWARF debug info to calculate frame sizes
4. DWARF says: "calculate() frame is 16 bytes, previous frame starts at RSP+16"
5. **If DWARF missing**: Profiler shows `[unknown]` or fails

**Real-World Profiling Output Examples:**

**With Frame Pointers (reliable):**
```
 50.2%  myapp  calculate()    # Clear function name
 30.1%  myapp  main()         # Complete call chain
 15.3%  myapp  _start()       # All frames resolved
```

**Without Frame Pointers + No DWARF (broken):**
```
 50.2%  myapp  [unknown]      # Lost function names
 30.1%  myapp  0x401234       # Just memory addresses
 15.3%  myapp  [unknown]      # Incomplete stack traces
```

**Without Frame Pointers + With DWARF (works but slower):**
```
 50.2%  myapp  calculate()    # DWARF provides function info
 30.1%  myapp  main()         # Profiler calculates frame sizes
 15.3%  myapp  _start()       # More CPU overhead to unwind
```

**Why This Matters for Performance Analysis:**
- **With FP**: You get complete, accurate stack traces → can identify bottlenecks
- **Without FP + No DWARF**: You get `[unknown]` functions → can't optimize effectively
- **Without FP + DWARF**: You get accurate traces but profiler uses more CPU
- **Production Trade-off**: 2-5% runtime cost vs. ability to profile effectively

#### **Java: Step-by-Step Example (`java MyApp`)**

**Step 1: Launch JVM ELF Executable**
- You run: `java MyApp`
- The OS loader loads the JVM executable binary (usually an ELF file such as `/usr/bin/java`) into memory, just like any other native program.
- The JVM ELF loads its required **native shared libraries** (e.g., `libjvm.so`, `libc.so`, etc.).
    - You can see these with `ldd $(which java)`.

**Step 2: JVM Initialization**
- JVM runs its own C/++ code to initialize the Java runtime.
- The JVM reserves/sets up heap, code cache, JIT structures.

**Step 3: JVM Loads Java Application**
- The JVM (which is already a running native process!) loads your `MyApp.class`.
- It does **not** map your `.class` files as ELF code—rather, the JVM reads them as data and parses Java bytecode instructions.

**Step 4: Running Java Code**
- Initially, Java bytecode is **interpreted** by the JVM or run in a basic mode.
- As certain methods/classes are called more frequently, the JVM uses its *JIT (Just-In-Time) compiler* to convert "hot" bytecode to *native machine code*. This is stored in a special private region of JVM memory (not a separate ELF file), optimized for performance.
    - These memory regions can be seen in `/proc/<pid>/maps` for the JVM process, labeled as "[anon]" or similar.
- **Execution truly begins** at `public static void main(String[] args)` _inside_ the JVM process.

**Summary Table:**
| Component             | How/When It’s Loaded                                    |
|-----------------------|--------------------------------------------------------|
| JVM ELF binary        | OS loader maps into memory at process start             |
| Shared libraries      | OS loader loads (e.g., `libjvm.so`, `libc.so`)         |
| MyApp.class (bytecode)| JVM reads/parses as data; *not directly loaded by OS*  |
| JIT-compiled code     | JVM writes optimized native code to memory at runtime   |

**So:**
- The OS only *directly* loads native ELF binaries/libraries (JVM, dependencies).
- Your bytecode is handled entirely by the running Java process, which dynamically converts it to native code for the CPU.

**Key point:** JVM ELF and shared libs are mapped at OS start—the user Java code (`.class`, `.jar`) is only ever *parsed/interpreted and/or JIT-compiled* by the running JVM, not mapped as native code.

---

#### **Why Frame Pointers are Crucial for Profiling**

- **What is a Frame Pointer?**
    - A special register (e.g., `%rbp` on x86_64) saving the address of the previous stack frame.
    - The compiler typically emits prologue/epilogue code like:
      ```asm
      push %rbp           # save current FP value
      mov %rsp, %rbp      # establish new frame pointer
      ... function body ...
      pop %rbp
      ret
      ```
    - Each stack frame points to the one below, making a linked list.

- **With Frame Pointers (FP enabled):**
    - Stack is easy to unwind for debuggers/profilers; just follow the chain from `rbp` backwards.
    - **Profiles and stack traces are deep and complete.**

- **Without Frame Pointers (FP omitted):**
    - Compiler is free to reuse `rbp` for other data, omitting that linked list.
    - To unwind, profilers must use DWARF data, heuristics, or often give up—resulting in shallow, unreliable call stacks.
    - **Call stacks will have '[unknown]' or be truncated.**

**ASCII Example: What Stacks Look Like With vs. Without Frame Pointers**

*With frame pointers (linked list in memory)*:
```
Stack top
+------------------------+
| return addr: foo()     |
| prev FP: [0x7ffd123450]|
+------------------------+
| return addr: main()    |
| prev FP: [null/0x0]    |
+------------------------+
Stack bottom
```
The profiler reads current FP, reads previous FP, and so on.

*Without frame pointers (no linked list; compiler may use rbp for anything!):*
```
Stack top
+---------------------+
| return addr: ???    |
| ...inlined frames   |
+---------------------+
   (unwind fails or requires DWARF parsing)
```

- **With FP:**
    - Easy, reliable stack unwinding for tools like `perf`, `gprofiler`, etc.
- **Without FP:**
    - Unreliable! Some stacks are shallow or missing entirely.
    - Profiler output may look like:
      ```
      doImportantWork()
      [unknown]
      main
      ```
- **For Java:**
    - async-profiler walks Java call stacks reliably using the JVM’s own metadata and special APIs (JVMTI), even without native FPs, because the JVM maintains the stack chain for the interpreter/JIT.
    - Native frames beneath Java frames, however, do still require FPs for native unwinding reliability.

---

### 2. Step-by-Step Trace Collection (Applies to C++, Java, Native)

#### **Step 1: Profiler Event Setup and Trigger**
- The profiler (e.g., perf, async-profiler) sets a periodic timer or hardware event (CPU cycles, walltime, etc).
- **perf** sets up a kernel event (perf_event_open). **async-profiler** requests OS to deliver signals (SIGPROF) to JVM.

#### **Step 2: Sampling Interrupt**
- When the timer/event fires, the OS or CPU delivers an interrupt:
  - **perf:** Non-Maskable Interrupt (NMI) from hardware
  - **async-profiler:** Signal handler invoked in Java thread

#### **Step 3: Stack Unwinding Begins**
- The profiler's handler/interrupt runs in the context of the interrupted thread.
- It reads the **Instruction Pointer (IP):** Indicates the currently executing code address.
- It reads the **Stack Pointer (SP):** Marks the top of the call stack.
- **Importance of Frame Pointer (FP):**
    - **With FP:** Each frame stores a pointer to the previous frame (simple linked-list walk).
    - **No FP (frame pointer omission):** The profiler must decode debug data (DWARF) or use heuristics, which is slower and incomplete.
    - **Example:**
        - With FP: main() → funcA() → funcB() can be reliably walked.
        - Without FP: Unwinding may stop mid-stack (e.g., only see funcB), or encounter '[unknown]'.

#### **Step 4: Stack Frame Walk**
- Native unwinding: Repeatedly read the previous frame pointer, push return addresses to the call stack until you reach the base (main/start).
- DWARF-based: Follow debug table instructions for each function, more CPU/memory intensive.
- **JVM Special Case:** async-profiler uses JVMTI/HotSpot APIs (`AsyncGetCallTrace`)—the JVM knows Java code structure, even for inlined methods or JIT.

#### **Step 5: Symbol Resolution (Address → Function Name)**
- Map each captured program counter (PC) to a symbolic name:
  - Native: Use ELF symbol/dwarf tables.
  - Java: JVM holds symbol tables for all loaded classes; async-profiler queries HotSpot.
  - JIT/native: Tools like perf-map-agent extract just-in-time compiled symbol info.

#### **Step 6: Aggregation and Frequency Counting**
- Each unique stack trace is aggregated: Sample counts show which code paths are "hot."
- Example (collapsed stack format):
    ```
    main;process;parse  1020
    main;other         200
    ```

#### **Step 7: Output / Visualization**
- Human-readable formats produced: flamegraphs, call trees, text reports, interactive UIs.

---

### 3. **With vs Without Frame Pointer: Examples**

#### **With Frame Pointer (FP)**
- Every stack frame contains a pointer to the previous frame and the return address.
- The stack trace looks complete and deep: `main→handle→parse→doStuff`.

#### **Without Frame Pointer (FPO)**
- No guaranteed linked-list structure.
- Native unwinder may only find part of the stack, see '[unknown]', or terminate early:
    ```
    doStuff()
    [unknown]
    [unknown]
    ```

---

### 4. **Why IP and SP Are Needed**
- **Instruction Pointer (IP):** Tells the profiler which instruction was executing when interrupted (for leaf function identification).
- **Stack Pointer (SP):** Tells the stack-walking routine where the call chain resides in memory—the base pointer for unwinding.

---

### 5. **Java: Interrupts, JVM, and async-profiler**

#### **async-profiler (Java-aware):**
- **Interrupt Method**: Sends `SIGPROF` signal to the JVM process
- **Signal Handling**: Caught by async-profiler's custom signal handler running in user space (not kernel NMI)
- **Stack Walking**: Uses HotSpot/JVM internal APIs (JVMTI) to directly access Java call frames
- **Symbol Resolution**: Directly resolves Java method names, line numbers, and even inlined JIT code
- **Native Code**: For native frames (C/C++ in JVM), uses frame pointers if available (`-XX:+PreserveFramePointer`)

#### **perf (Generic system profiler):**
- **Interrupt Method**: Uses hardware Performance Monitoring Unit (PMU) with NMI interrupts
- **Signal Handling**: Kernel-level interrupt handling (no user-space cooperation)
- **Stack Walking**: Hardware-based unwinding using frame pointers or DWARF debug info only
- **Symbol Resolution Challenge**: Cannot directly understand JIT-compiled Java methods
- **Java Method Resolution**: Requires additional tools:
  - **perf-map-agent**: Generates `/tmp/perf-<pid>.map` files mapping JIT addresses to Java method names
  - **JVM flags**: `-XX:+PreserveFramePointer` and `-XX:+UnlockDiagnosticVMOptions -XX:+DebugNonSafepoints`

#### **Difference in Stack Example**
- **perf with FP (C++):**
    ```
    main
      └─handle()
         └─processRequest()
            └─parseData()
    ```
- **perf w/o FP or missing symbols:**
    ```
    main
      └─handle()
         └─[unknown]
         └─[unknown]
    ```
- **Java async-profiler:**
    ```
    MyApp.main()
      └─doWork()
         └─parseJson()
    ```
  ...including JVM inlined methods and native frames if frame pointer available.

---

### 6. **Summary Table: Profiling Mechanisms Compared**

| Step | perf (System-wide) | async-profiler (Java-specific) |
|------|-------------------|--------------------------------|
| **1. Interrupt Source** | Hardware PMU + NMI (kernel) | SIGPROF signal (user space) |
| **2. Stack Collection** | Hardware unwinding (FP/DWARF) | JVMTI + HotSpot APIs |
| **3. Symbol Resolution** | ELF symbols + perf-map files | Direct Java method access |
| **4. Code Understanding** | Native code only | Java + JIT + native code |

#### **Detailed Comparison:**

| Aspect | perf | async-profiler |
|--------|------|----------------|
| **Java Method Names** | Requires perf-map-agent | Built-in (direct JVM access) |
| **JIT Code Profiling** | Limited (needs symbol mapping) | Full support (inlined methods) |
| **Line Numbers** | DWARF debug info only | Java source lines + bytecode |
| **Overhead** | Very low (~1%) | Low (~2-3%) |
| **Setup Complexity** | High (JVM flags + agents) | Simple (single tool) |
| **Multi-language** | All languages | Java + some native |

#### **Example Output Comparison:**

**perf (without perf-map-agent):**
```
 50.2%  java  [unknown]         [.] 0x00007f8b2c4a1234
 25.1%  java  [unknown]         [.] 0x00007f8b2c4a5678
```

**perf (with perf-map-agent):**
```
 50.2%  java  [JIT] tid 1234    [.] com.example.MyClass::processData
 25.1%  java  [JIT] tid 1234    [.] com.example.Parser::parseJson
```

**async-profiler:**
```
 50.2%  com.example.MyClass::processData:142
 25.1%  com.example.Parser::parseJson:89
  15.3%  com.example.Utils::validateInput:23
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
