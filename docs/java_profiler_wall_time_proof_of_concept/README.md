# Wall Time vs CPU Time Profiling - Proof of Concept

This directory contains a comprehensive test application that demonstrates and validates the wall time profiling feature for Java async-profiler in gprofiler.

## 🎯 Purpose

This proof-of-concept validates that wall time profiling successfully captures I/O waits, lock contention, and blocking operations that are invisible to CPU-only profiling.

## What This Application Does

### CPU-Intensive Operations (High CPU Time)
- `doCpuIntensiveWork()` - Mathematical calculations (sqrt, sin, cos)
- `calculatePrimes()` - Prime number calculations
- These will show **prominently in CPU profiling** but **less prominently in wall time profiling**

### I/O Blocking Operations (High Wall Time, Low CPU Time)  
- `simulateFileIo()` - File read/write operations
- `simulateDatabaseQuery()` - Thread.sleep() simulating DB queries
- `simulateNetworkDelay()` - Network timeout simulation
- `doLockContentionWork()` - Synchronized block contention
- These will show **prominently in wall time profiling** but **barely in CPU profiling**

## Quick Start

```bash
# Build and run the test application
cd /home/prashantpatel/code/pinterest-opensource/test-wall-time
docker-compose up --build -d

# Check if it's running
docker ps
curl http://localhost:9090  # Should get a response
```

## Testing with gprofiler

### 1. CPU Time Profiling (will miss I/O waits)
```bash
sudo ./build/x86_64/gprofiler \
  --java-async-profiler-mode cpu \
  -d 60 \
  -o /tmp/cpu-profile \
  --log-level DEBUG \
  --service-name cpu-test
```

**Expected CPU Profile Results:**
- ✅ `doCpuIntensiveWork` - **High sample count**
- ✅ `calculatePrimes` - **High sample count**  
- ❌ `simulateFileIo` - **Low/missing sample count**
- ❌ `simulateDatabaseQuery` - **Low/missing sample count**

### 2. Wall Time Profiling (will capture I/O waits)
```bash
sudo ./build/x86_64/gprofiler \
  --java-async-profiler-mode wall \
  -d 60 \
  -o /tmp/wall-profile \
  --log-level DEBUG \
  --service-name wall-test
```

**Expected Wall Profile Results:**
- ✅ `doCpuIntensiveWork` - **High sample count** (still CPU work)
- ✅ `calculatePrimes` - **High sample count** (still CPU work)
- ✅ `simulateFileIo` - **High sample count** ⭐ (I/O waits captured!)
- ✅ `simulateDatabaseQuery` - **High sample count** ⭐ (Sleep waits captured!)
- ✅ `doLockContentionWork` - **High sample count** ⭐ (Lock waits captured!)

## Expected Results & Validation

### **Flamegraph Analysis:**
Wall time profiling should show a flamegraph similar to this structure:

```
Left Side (Orange/Brown) - I/O Operations:
├── WallTimeTestApp.simulateNetworkDelay() ← Large section (I/O waits)
├── java/net/Socket.connect() ← Network timeouts  
├── java/net/SocketImpl.connect() ← Socket operations
└── NET_Poll, __poll ← System I/O polling

Right Side (Purple) - CPU + Blocking:
├── WallTimeTestApp.doCpuIntensiveWork() ← CPU operations
├── WallTimeTestApp.doLockContentionWork() ← Lock contention
├── java/util/concurrent operations ← Thread sync
└── JVM_Sleep, pthread_cond_timedwait ← Sleep/wait
```

### **Key Success Indicators:**
- ✅ **Large I/O sections** visible in wall time flamegraph
- ✅ **Network operations** (`Socket.connect`) show significant time
- ✅ **Lock contention** (`doLockContentionWork`) captured
- ✅ **Sleep operations** (`JVM_Sleep`) visible
- ✅ **CPU operations** still present but proportionally smaller

## Compare Results

```bash
# Compare the collapsed stack files
echo "=== CPU Profile Top Methods ==="
grep -E "(doCpuIntensiveWork|simulateFileIo|simulateDatabaseQuery)" /tmp/cpu-profile/*.col | head -10

echo "=== Wall Profile Top Methods ==="  
grep -E "(doCpuIntensiveWork|simulateFileIo|simulateDatabaseQuery)" /tmp/wall-profile/*.col | head -10
```

## What You Should See

**Key Difference:** Wall time profiling should show **significantly higher sample counts** for:
- File I/O operations (`simulateFileIo`)
- Database simulation (`simulateDatabaseQuery`) 
- Lock contention (`doLockContentionWork`)
- Network timeouts (`simulateNetworkDelay`)

While CPU profiling will primarily show:
- Mathematical operations (`doCpuIntensiveWork`)
- Prime calculations (`calculatePrimes`)

## Cleanup

```bash
docker-compose down
docker rmi test-wall-time_wall-time-test-app
```
