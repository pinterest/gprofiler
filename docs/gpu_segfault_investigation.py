#!/usr/bin/env python3
"""
GPU Segfault Investigation Script
Helps understand why perf script segfaults on GPU machines
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def run_command(cmd, timeout=30):
    """Run a command and capture output, with timeout"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except Exception as e:
        return -999, "", str(e)


def test_perf_record_and_script(perf_binary, output_file, extra_args="", duration=5):
    """Test perf record and script with specific configuration"""
    print(f"\n=== Testing {perf_binary} ===")

    # Record phase
    record_cmd = (
        f"sudo {perf_binary} record -o {output_file} -g -e cpu-clock --freq=11 -a {extra_args} sleep {duration}"
    )
    print(f"Recording: {record_cmd}")

    ret_code, stdout, stderr = run_command(record_cmd)
    if ret_code != 0:
        print(f"❌ Recording failed: {ret_code}")
        print(f"stderr: {stderr}")
        return False

    print(f"✅ Recording successful: {stdout.strip()}")

    # Check file size
    try:
        file_size = os.path.getsize(output_file)
        print(f"📊 File size: {file_size:,} bytes")
    except:
        print("❌ Cannot get file size")
        return False

    # Script phase - this is where segfaults happen
    script_cmd = f"sudo timeout 10s {perf_binary} script -i {output_file} -F +pid"
    print(f"Parsing: {script_cmd}")

    ret_code, stdout, stderr = run_command(script_cmd, timeout=15)

    if ret_code < 0:
        signal_num = -ret_code
        if signal_num == 11:  # SIGSEGV
            print(f"💥 SEGFAULT detected! Signal {signal_num}")
            return False
        else:
            print(f"🔶 Died with signal {signal_num}")
            return False
    elif ret_code > 0:
        print(f"❌ Script failed with exit code {ret_code}")
        print(f"stderr: {stderr}")
        return False
    else:
        lines = len(stdout.splitlines()) if stdout else 0
        print(f"✅ Script successful: {lines} lines of output")
        if lines > 0:
            print(f"📋 First few lines:\n{stdout[:200]}...")
        return True


def check_gpu_activity():
    """Check if GPU is active which might influence perf behavior"""
    print("\n=== GPU Activity Check ===")
    ret_code, stdout, stderr = run_command(
        "nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits"
    )
    if ret_code == 0:
        print(f"GPU utilization: {stdout.strip()}")
        return stdout.strip()
    else:
        print("❌ Cannot check GPU status")
        return None


def investigate_file_format(problematic_file):
    """Investigate why the original file has format issues"""
    print(f"\n=== File Format Investigation: {problematic_file} ===")

    if not os.path.exists(problematic_file):
        print("❌ File does not exist")
        return

    # File command
    ret_code, stdout, stderr = run_command(f"sudo file {problematic_file}")
    print(f"file command: {stdout.strip()}")

    # Hexdump first few bytes
    ret_code, stdout, stderr = run_command(f"sudo hexdump -C {problematic_file} | head -5")
    print(f"hex dump:\n{stdout}")

    # Check if it's a valid perf data file
    ret_code, stdout, stderr = run_command(
        f"sudo /tmp/gprofiler-extracted/perf report --header-only -i {problematic_file} -v"
    )
    print(f"Header check result: {ret_code}")
    print(f"Header output: {stderr}")


def main():
    print("🔍 GPU Machine Perf Segfault Investigation")
    print("=" * 50)

    # System info
    print(f"Kernel: {subprocess.check_output(['uname', '-r']).decode().strip()}")

    # Check GPU
    gpu_status = check_gpu_activity()

    # Test system perf
    print("\n" + "=" * 50)
    print("🔬 TESTING SYSTEM PERF")
    success_system = test_perf_record_and_script("/usr/bin/perf", "/tmp/system_perf_test.data")

    # Test gProfiler perf
    print("\n" + "=" * 50)
    print("🔬 TESTING GPROFILER PERF")
    success_gprofiler = test_perf_record_and_script("/tmp/gprofiler-extracted/perf", "/tmp/gprofiler_perf_test.data")

    # Investigate problematic file
    problematic_file = "/tmp/gprofiler_tmp/tmp3mdm__cs/perf.fp"
    investigate_file_format(problematic_file)

    # Summary
    print("\n" + "=" * 50)
    print("📋 INVESTIGATION SUMMARY")
    print(f"System perf (5.4.157): {'✅ Success' if success_system else '❌ Failed'}")
    print(f"gProfiler perf (6.7.0): {'✅ Success' if success_gprofiler else '❌ Failed'}")

    if not success_system and not success_gprofiler:
        print("🔥 Both versions fail - likely GPU/kernel issue")
    elif success_gprofiler and not success_system:
        print("🎯 System perf fails, gProfiler works - version issue")
    elif success_system and not success_gprofiler:
        print("🤔 System perf works, gProfiler fails - unexpected!")
    else:
        print("✅ Both work in isolated tests - issue might be timing/load related")


if __name__ == "__main__":
    main()
