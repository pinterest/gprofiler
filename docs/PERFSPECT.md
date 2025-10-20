# 🔬 PerfSpect Integration Guide

## Overview

PerfSpect is Intel's performance analysis tool that provides Top-down Microarchitecture Analysis Method (TMAM) metrics to identify CPU pipeline bottlenecks. This guide explains how to interpret PerfSpect data collected by gProfiler and make optimization decisions.

## 🏗️ TMAM Fundamentals

### What is TMAM?

Top-down Microarchitecture Analysis Method (TMAM) is a systematic approach to identify performance bottlenecks in modern CPUs. It divides CPU pipeline utilization into four main categories:

```
CPU Pipeline Slots (100%)
├─ Frontend Bound (%)     - Can't fetch/decode instructions fast enough
├─ Backend Bound (%)      - Can't execute ready instructions  
├─ Bad Speculation (%)    - Wasted cycles on wrong predictions
└─ Retiring (%)          - Successfully completed useful work
```

### Pipeline Flow

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  Frontend   │───▶│   Backend   │───▶│ Execution   │───▶│ Retirement  │
│             │    │             │    │   Units     │    │             │
│ • Fetch     │    │ • Schedule  │    │ • ALU       │    │ • Commit    │
│ • Decode    │    │ • Dispatch  │    │ • FPU       │    │ • Results   │
│ • Predict   │    │ • Rename    │    │ • Load/Store│    │ • Arch State│
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
```

## 📊 Four TMAM Categories Explained

### 1. 🔄 Frontend Bound

**What it means:** CPU pipeline can't fetch and decode instructions fast enough to keep execution units busy.

**Root Causes:**
- **Instruction Cache Misses:** Code doesn't fit in L1 instruction cache
- **Branch Mispredictions:** CPU guessed wrong branch direction
- **Complex Instruction Decoding:** Instructions require multiple decode cycles
- **ITLB Misses:** Instruction Translation Lookaside Buffer misses

**Key Metrics to Monitor:**
```
Pipeline Utilization - Frontend Bound (%)
├─ Frontend Bound - Latency (%)    - Branch mispredicts, I-cache misses
└─ Frontend Bound - Bandwidth (%)  - Decode bottlenecks, fetch bandwidth

Supporting Metrics:
├─ Branch Misprediction Ratio
├─ Instruction Cache Fetch Miss Ratio  
├─ Op Cache Fetch Miss Ratio
└─ L1 ITLB Misses PTI
```

**Example Scenario:**
```cpp
// Frontend-bound code example
for (int i = 0; i < 1000000; i++) {
    if (random_condition(i)) {        // Unpredictable branches
        complex_function_a(i);        // Large instruction footprint
    } else {
        complex_function_b(i);        // More large functions
    }
}
```

### 2. ⚙️ Backend Bound

**What it means:** CPU has instructions decoded and ready but can't execute them due to resource limitations.

**Sub-categories:**
- **Memory Bound:** Waiting for data from cache/memory hierarchy
- **CPU Bound:** All execution units busy, resource contention

**Key Metrics to Monitor:**
```
Pipeline Utilization - Backend Bound (%)
├─ Backend Bound - Memory (%)      - Cache misses, memory latency
└─ Backend Bound - CPU (%)         - Execution unit saturation

Memory Bound Indicators:
├─ L1 Data Cache Fills PTI
├─ L2 Cache Misses PTI  
├─ L2 Cache Accesses from L1 Data Cache Misses PTI
├─ Remote DRAM Reads %
└─ L1/L2 DTLB Misses PTI

CPU Bound Indicators:
├─ Macro-ops Dispatched PTI
├─ Mixed SSE and AVX Stalls
└─ Pipeline Utilization - SMT Contention (%)
```

**Example Scenarios:**
```cpp
// Memory-bound example
for (int i = 0; i < size; i++) {
    result += large_array[random_indices[i]];  // Random memory access
}

// CPU-bound example  
for (int i = 0; i < size; i++) {
    result = complex_math_operation(result);   // Heavy computation
}
```

### 3. 🎯 Bad Speculation

**What it means:** CPU speculatively executed instructions that were later discarded due to incorrect predictions.

**Root Causes:**
- **Branch Mispredictions:** Wrong branch direction predicted
- **Pipeline Restarts:** Had to flush pipeline due to exceptions/interrupts

**Key Metrics to Monitor:**
```
Pipeline Utilization - Bad Speculation (%)
├─ Bad Speculation - Mispredicts (%)      - Branch prediction failures
└─ Bad Speculation - Pipeline Restarts (%) - Exception/interrupt overhead

Supporting Metrics:
└─ Branch Misprediction Ratio
```

**Example Scenario:**
```cpp
// High bad speculation code
class Base { public: virtual void process() = 0; };
class A : public Base { public: void process() override { /* work */ } };
class B : public Base { public: void process() override { /* work */ } };

std::vector<std::unique_ptr<Base>> objects;
for (auto& obj : objects) {
    obj->process();  // Unpredictable virtual calls
}
```

### 4. ✅ Retiring

**What it means:** CPU successfully completed useful work and committed results to architectural state.

**Sub-categories:**
- **Fastpath:** Simple instructions executed efficiently
- **Microcode:** Complex instructions requiring multiple micro-ops

**Key Metrics to Monitor:**
```
Pipeline Utilization - Retiring (%)
├─ Retiring - Fastpath (%)     - Simple, efficient instructions
└─ Retiring - Microcode (%)    - Complex instructions (string ops, etc.)

Supporting Metrics:
├─ Macro-ops Retired PTI
└─ IPC (Instructions Per Cycle)
```

**Good vs. Concerning Patterns:**
```
✅ Good:    Retiring > 25%, Fastpath dominant
⚠️  Warning: Retiring < 15%, high Microcode %
❌ Bad:     Retiring < 10%, other categories > 80%
```

## 🎯 Interpretation Framework

### Performance Health Matrix

```
Retiring %    Frontend %   Backend %    Bad Spec %   Assessment
> 30%         < 20%        < 30%        < 20%        🟢 Excellent
20-30%        20-40%       30-50%       20-30%       🟡 Good  
15-20%        40-60%       50-70%       30-40%       🟠 Needs Attention
< 15%         > 60%        > 70%        > 40%        🔴 Critical Issues
```

### Optimization Priority Matrix

```
Primary Bottleneck          Secondary Issues         Optimization Focus
Frontend > 50%              Any                      Code layout, branching
Backend-Memory > 40%        Any                      Data access patterns  
Backend-CPU > 40%           Low retiring             Algorithm efficiency
Bad Speculation > 30%       Any                      Branch predictability
Low Retiring < 15%          High others              Fundamental redesign
```

## 📈 Real-World Examples

### Example 1: Well-Optimized Application

```
Metric                                          Value    Assessment
Pipeline Utilization - Frontend Bound (%)      18.2     🟢 Good
Pipeline Utilization - Backend Bound (%)       31.5     🟡 Acceptable  
Pipeline Utilization - Bad Speculation (%)     12.1     🟢 Excellent
Pipeline Utilization - Retiring (%)            38.2     🟢 Excellent
├─ Retiring - Fastpath (%)                     35.1     🟢 Efficient
└─ Retiring - Microcode (%)                    3.1      🟢 Low overhead

Cache Performance:
L2 Cache Hit Rate                              96.8%    🟢 Excellent
Branch Misprediction Ratio                     2.1%     🟢 Good
IPC                                            2.84     🟢 High efficiency
```

**✅ Recommendations:**
- **Instance Type:** Current setup is optimal
- **Scaling:** Horizontal scaling recommended for increased throughput
- **Optimizations:** Minor - focus on maintaining current efficiency

### Example 2: Frontend-Bound Application

```
Metric                                          Value    Assessment
Pipeline Utilization - Frontend Bound (%)      67.3     🔴 Critical
├─ Frontend Bound - Latency (%)                45.2     🔴 Branch issues
└─ Frontend Bound - Bandwidth (%)              22.1     🟠 Decode issues
Pipeline Utilization - Backend Bound (%)       18.4     🟢 Good
Pipeline Utilization - Bad Speculation (%)     8.9      🟢 Good  
Pipeline Utilization - Retiring (%)            5.4      🔴 Very low

Branch/Cache Performance:
Branch Misprediction Ratio                     18.7%    🔴 Very high
Instruction Cache Fetch Miss Ratio             12.3%    🔴 High
L1 ITLB Misses PTI                            156.2     🔴 High
IPC                                            0.67     🔴 Very low
```

**🎯 Optimization Recommendations:**

**Immediate Actions:**
1. **Profile-Guided Optimization (PGO)**
   ```bash
   # Enable PGO compilation
   gcc -fprofile-generate -O3 app.c -o app
   ./app < training_data
   gcc -fprofile-use -O3 app.c -o app_optimized
   ```

2. **Code Layout Optimization**
   ```cpp
   // Before: Unpredictable branches
   if (unlikely_condition()) { large_cold_function(); }
   
   // After: Use branch hints and cold attributes
   if (__builtin_expect(condition, 0)) { 
       __attribute__((cold)) large_cold_function(); 
   }
   ```

3. **Function Inlining and Size Reduction**
   ```cpp
   // Reduce code size, increase locality
   inline small_hot_functions();
   __attribute__((noinline)) large_cold_functions();
   ```

**Instance Recommendations:**
- **Current:** General purpose instances (M6i, M7i)  
- **Better:** Compute optimized with larger L1I cache (C6i, C7i)
- **Avoid:** Memory optimized instances (waste of resources)

### Example 3: Memory-Bound Application

```
Metric                                          Value    Assessment
Pipeline Utilization - Frontend Bound (%)      12.1     🟢 Good
Pipeline Utilization - Backend Bound (%)       78.9     🔴 Critical
├─ Backend Bound - Memory (%)                  71.2     🔴 Memory bottleneck
└─ Backend Bound - CPU (%)                     7.7      🟢 Good
Pipeline Utilization - Bad Speculation (%)     4.3      🟢 Good
Pipeline Utilization - Retiring (%)            4.7      🔴 Very low

Memory Performance:
L2 Cache Misses PTI                            89.3     🔴 Very high
L1 Data Cache Fills from DRAM PTI              23.4     🔴 High
Remote DRAM Reads %                            15.7     🔴 NUMA issues
L2 DTLB Misses PTI                            45.6     🔴 TLB pressure
4KB Page DTLB Activity %                       87.2     🔴 Small pages
```

**🎯 Optimization Recommendations:**

**Memory Access Optimization:**
1. **Data Structure Redesign**
   ```cpp
   // Before: Poor cache locality
   struct BadLayout {
       int frequently_used;
       char padding[60];      // Cache line waste
       int also_frequent;
   };
   
   // After: Cache-friendly layout
   struct GoodLayout {
       int frequently_used;
       int also_frequent;     // Same cache line
       char padding[56];      // Explicit padding
   } __attribute__((aligned(64)));
   ```

2. **Memory Prefetching**
   ```cpp
   for (int i = 0; i < size; i++) {
       __builtin_prefetch(&data[i + 8], 0, 3);  // Prefetch ahead
       process(data[i]);
   }
   ```

3. **Large Page Configuration**
   ```bash
   # Enable 2MB huge pages
   echo 1024 > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages
   
   # Application: Use madvise for large allocations
   madvise(ptr, size, MADV_HUGEPAGE);
   ```

**Instance Recommendations:**
- **Current:** Likely general purpose  
- **Better:** Memory optimized (R6i, R7i, X2i) with high memory bandwidth
- **Consider:** Instances with larger L3 cache and more memory channels

### Example 4: CPU-Bound Application

```
Metric                                          Value    Assessment
Pipeline Utilization - Frontend Bound (%)      8.7      🟢 Good
Pipeline Utilization - Backend Bound (%)       82.1     🔴 Critical
├─ Backend Bound - Memory (%)                  12.3     🟢 Good
└─ Backend Bound - CPU (%)                     69.8     🔴 Execution bottleneck
Pipeline Utilization - Bad Speculation (%)     3.4      🟢 Good
Pipeline Utilization - Retiring (%)            5.8      🔴 Very low

Execution Performance:
Macro-ops Dispatched PTI                       245.6    🔴 High dispatch rate
Mixed SSE and AVX Stalls                       34.2     🔴 Vector unit contention
Pipeline Utilization - SMT Contention (%)     28.7     🔴 Thread competition
IPC                                            0.41     🔴 Very low
```

**🎯 Optimization Recommendations:**

**Algorithmic Optimization:**
1. **Vectorization**
   ```cpp
   // Before: Scalar operations
   for (int i = 0; i < size; i++) {
       result[i] = sqrt(input[i] * 2.0f);
   }
   
   // After: SIMD vectorization
   #pragma omp simd
   for (int i = 0; i < size; i += 8) {
       __m256 vec = _mm256_load_ps(&input[i]);
       vec = _mm256_mul_ps(vec, _mm256_set1_ps(2.0f));
       vec = _mm256_sqrt_ps(vec);
       _mm256_store_ps(&result[i], vec);
   }
   ```

2. **Reduce Instruction Complexity**
   ```cpp
   // Before: Complex operations
   result = pow(x, 3) + sqrt(y) + sin(z);
   
   // After: Optimized alternatives
   result = x * x * x + fast_sqrt(y) + sin_approx(z);
   ```

**Instance Recommendations:**
- **Current:** Likely general purpose
- **Better:** Compute optimized (C6i, C7i) with higher CPU frequency
- **Consider:** Instances with more execution units and higher IPC

## 🔧 gProfiler Integration

### Enabling PerfSpect Collection

```bash
# Install PerfSpect (requires root/sudo)
git clone https://github.com/intel/perfspect.git
cd perfspect && sudo python setup.py install

# Run gProfiler with hardware metrics
gprofiler --enable-hw-metrics-collection \
          --perfspect-path /usr/local/bin/perfspect \
          --perfspect-duration 60 \
          --duration 300
```

### Configuration Options

```bash
--enable-hw-metrics-collection    # Enable PerfSpect integration
--perfspect-path PATH            # Path to PerfSpect binary  
--perfspect-duration SECONDS     # Collection interval (default: 60s)
```

### Data Collection Flow

```
gProfiler Runtime
├─ Profile Collection (continuous)
├─ PerfSpect Metrics (every 60s)
│  ├─ Raw CSV data
│  ├─ Summary CSV  
│  └─ HTML report
└─ Combined Analysis (flamegraph + TMAM)
```

## 🎯 Decision Framework

### 1. Instance Type Selection

```
TMAM Pattern                    Recommended Instance Family
Frontend > 50%                  C6i/C7i (better branch prediction)
Backend-Memory > 50%            R6i/R7i/X2i (high memory bandwidth)  
Backend-CPU > 50%              C6i/C7i (more execution units)
Retiring > 30%                 Current instance optimal
Mixed bottlenecks              M6i/M7i (balanced)
```

### 2. Horizontal vs Vertical Scaling

```
Scaling Decision Matrix:
                    Vertical Scale Up    Horizontal Scale Out
Retiring > 25%           ❌ Wasteful         ✅ Recommended
Frontend Bound           ✅ Better CPU        ❌ Won't help
Backend-Memory           ✅ More memory       ❌ Won't help  
Backend-CPU             ✅ Faster CPU        ❌ Won't help
Bad Speculation         ✅ Better prediction  ❌ Won't help
```

### 3. Software Optimization Priority

```
Priority 1 (Critical): Retiring < 15% OR any category > 70%
Priority 2 (High):      Retiring 15-20% OR any category > 50%  
Priority 3 (Medium):    Retiring 20-25% OR any category > 40%
Priority 4 (Low):       Retiring > 25% AND all categories < 40%
```

## 📚 Additional Resources

### Intel Documentation
- [Intel VTune Profiler TMAM Guide](https://software.intel.com/content/www/us/en/develop/documentation/vtune-help/top/analyze-performance/microarchitecture-exploration-analysis.html)
- [PerfSpect GitHub Repository](https://github.com/intel/perfspect)

### Optimization Guides  
- [Intel Optimization Reference Manual](https://software.intel.com/content/www/us/en/develop/articles/intel-sdm.html)
- [Agner Fog's Optimization Manuals](https://www.agner.org/optimize/)

### Performance Analysis Tools
- Intel VTune Profiler (GUI alternative)
- Linux perf with TMAM support
- Intel Performance Counter Monitor (PCM)

## 🔍 Troubleshooting

### Missing TMAM Data (NaN values)

**Possible Causes:**
1. **Insufficient Permissions:** PerfSpect requires root or perf_event access
2. **Unsupported Hardware:** TMAM requires Intel processors with specific PMU support
3. **Virtualized Environment:** Some cloud instances don't expose PMU counters
4. **Short Collection Time:** Increase `--perfspect-duration` for stable metrics

**Solutions:**
```bash
# Check PMU support
cat /proc/cpuinfo | grep -i "model name"
ls /sys/devices/cpu/events/

# Verify perf_event access  
echo 0 | sudo tee /proc/sys/kernel/perf_event_paranoid

# Test PerfSpect directly
perfspect metrics --duration 30
```

### Interpreting Incomplete Data

When only some TMAM metrics are available:
- Focus on available cache/TLB metrics for memory analysis
- Use IPC and CPI for general efficiency assessment  
- Correlate with application-level flamegraphs
- Consider upgrading to bare-metal instances for full PMU access
