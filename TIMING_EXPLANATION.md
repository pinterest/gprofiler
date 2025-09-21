# GProfiler Memory Leak Timing Explanation

## The Problem: Snapshot Duration > Configured Duration

### Current Configuration
- **Configured Duration**: 120 seconds (`-d 120`)
- **Actual Snapshot Time**: 123.7 seconds (consistently)
- **Overtime**: +3.7 seconds per snapshot

### Timeline Visualization

```
Normal Scenario (Snapshot < Duration):
Time:  0    60   120   180   240
       |----S----|     |----S----|
       Snapshot  Wait  Snapshot  Wait
                Memory         Memory
                Cleanup        Cleanup

Problem Scenario (Snapshot > Duration):
Time:  0    60   120  123.7  247.4
       |------S------|------S------|
       Snapshot123.7s Snapshot123.7s
                     NO WAIT TIME!
                     NO MEMORY CLEANUP!
```

### Code Flow Explanation

```python
# In run_continuous() method:

# Step 1: Start snapshot
snapshot_start = time.monotonic()  # Record start time

# Step 2: Execute snapshot (ALWAYS COMPLETES)
self._snapshot()  # Takes 123.7 seconds (ALL profiling, merging, uploading)

# Step 3: Calculate how long snapshot took
snapshot_duration = time.monotonic() - snapshot_start  # 123.7 seconds

# Step 4: Calculate remaining wait time
remaining_wait = max(self._duration - snapshot_duration, 0)
# remaining_wait = max(120 - 123.7, 0) = max(-3.7, 0) = 0

# Step 5: Wait for remaining time
self._profiler_state.stop_event.wait(remaining_wait)  # wait(0) = NO WAIT!

# Step 6: Loop continues immediately - START NEXT SNAPSHOT
```

### Memory Accumulation Pattern

Each snapshot creates large objects:
- **Profile data**: 50+ MB strings
- **Process profiles**: Hundreds of MB
- **Merged results**: Combined data
- **System metrics**: Additional overhead

**Normal flow**:
```
Snapshot creates data → Memory cleanup → Wait period → Memory stable → Next snapshot
   Peak: 200MB           150MB          150MB         150MB         200MB
```

**Problem flow**:
```
Snapshot creates data → Next snapshot starts immediately → More data created
   Peak: 200MB            Already at 200MB               Peak: 400MB+
```

### Why Snapshots Take So Long

Looking at your process tree, snapshots are slow because:

1. **Java profiling**: 2 Java processes taking ~60s each
2. **Python profiling**: PyPerf processes running
3. **Perf recording**: System-wide profiling
4. **Large profile data**: 50+ MB profile strings
5. **Network upload**: 10s timeout, large data upload
6. **Profile merging**: CPU-intensive string operations

### The Solution

We've implemented several fixes:

1. **Aggressive memory cleanup**: Start at 150MB instead of 800MB
2. **Multiple GC rounds**: 3 rounds of garbage collection
3. **Fewer threads**: 2 workers instead of 4 (less memory per thread)
4. **Proactive cleanup**: GC after profile merging
5. **Emergency cleanup**: Force GC when overlap detected
6. **Memory monitoring**: Track memory delta per snapshot

### Expected Behavior After Fix

With aggressive memory management:
```
Snapshot (123.7s) → Emergency cleanup → No wait → Next snapshot
   Peak: 200MB        Forced down to     Still 150MB    Peak: 200MB
                      150MB via GC
```

The key insight: **We can't make snapshots faster, but we can manage memory better during overlapping runs.**
