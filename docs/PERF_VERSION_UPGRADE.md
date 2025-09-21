# Perf Version Upgrade - GPU Segfault Fix

## Problem Summary

gProfiler was experiencing segmentation faults on GPU machines due to perf version incompatibility between the bundled perf 6.7.0 (February 2024) and older kernels like 5.4.

## Root Cause

- **Issue**: Not GPU-specific, but perf version incompatibility
- **Evidence**: System perf 5.4.157 works fine, gProfiler's perf 6.7.0 segfaults
- **Location**: `perf script` command crashes during data parsing

## Solution: Perf Version Upgrade

### What Changed

**Before**: 
- Perf version: Linux v6.7 (commit `5c103bf97fb268e4ea157f5e1c2a5bd6ad8c40dc`)
- Date: February 12, 2024
- Issue: Segfaults on kernel 5.4 machines

**After**:
- Perf version: Latest from Granulate/linux (commit `9909d736d8b8927d79003dfa9732050a08c11221`)
- Date: October 22, 2024  
- Benefits: 8+ months of bug fixes and compatibility improvements

### Files Modified

1. **`scripts/perf_build.sh`**:
   - Updated commit hash for newer perf version
   - Maintains self-contained approach (static linking)

### Why This Approach

✅ **Maintains Self-Contained Design**: gProfiler continues to bundle all binaries
✅ **No System Dependencies**: Still builds statically linked perf binary  
✅ **Backward Compatible**: Graceful error handling already implemented as fallback
✅ **Long-term Fix**: Addresses root cause rather than symptoms

### Testing Recommendations

1. **Build new gProfiler** with updated perf version
2. **Test on GPU machines** that previously failed:
   ```bash
   sudo ./build/x86_64/gprofiler -u -o /tmp/results -d 60 --server-host <your-server>
   ```
3. **Verify no segfaults** in perf script execution
4. **Confirm profiling data** is collected successfully

### Fallback Strategy

If the upgrade doesn't completely resolve issues:
- Graceful error handling is already implemented
- Can fall back to alternative profiling methods
- Investigation scripts available in `docs/` directory

### Technical Details

- **Build Process**: Unchanged - still downloads, builds, and statically links perf
- **Container Size**: Minimal impact (same perf binary, just newer version)
- **Dependencies**: None - completely self-contained
- **Compatibility**: Better kernel version compatibility expected

This upgrade maintains gProfiler's self-contained agent architecture while fixing the GPU machine segfault issue through improved perf compatibility.