#!/usr/bin/env python3
"""
Enhanced Perf Fallback Strategy for gProfiler
Handles version incompatibilities gracefully
"""

import logging
import subprocess
from pathlib import Path


def get_kernel_version():
    """Get running kernel version"""
    try:
        result = subprocess.run(["uname", "-r"], capture_output=True, text=True)
        return result.stdout.strip()
    except:
        return None


def test_perf_compatibility(perf_binary, test_duration=2):
    """Test if perf binary works with current kernel"""
    try:
        # Quick test record
        cmd = [
            perf_binary,
            "record",
            "-o",
            "/tmp/perf_test.data",
            "-e",
            "cpu-clock",
            "--freq=11",
            "sleep",
            str(test_duration),
        ]

        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode != 0:
            return False

        # Test script parsing
        script_cmd = [perf_binary, "script", "-i", "/tmp/perf_test.data", "-F", "+pid"]
        result = subprocess.run(script_cmd, capture_output=True, timeout=10)

        # Clean up
        Path("/tmp/perf_test.data").unlink(missing_ok=True)

        return result.returncode == 0

    except Exception as e:
        logging.warning(f"Perf compatibility test failed: {e}")
        return False


def select_perf_binary():
    """Select the best perf binary for current environment"""

    # Try bundled perf first
    bundled_perf = "/tmp/gprofiler-extracted/perf"
    system_perf = "/usr/bin/perf"

    kernel_version = get_kernel_version()
    logging.info(f"Kernel version: {kernel_version}")

    # Test bundled perf
    if Path(bundled_perf).exists():
        if test_perf_compatibility(bundled_perf):
            logging.info("Using bundled perf (6.7.0) - compatibility confirmed")
            return bundled_perf
        else:
            logging.warning("Bundled perf failed compatibility test")

    # Fallback to system perf
    if Path(system_perf).exists():
        if test_perf_compatibility(system_perf):
            logging.info("Using system perf - compatibility confirmed")
            return system_perf
        else:
            logging.error("System perf also failed compatibility test")

    # Last resort
    logging.error("No compatible perf binary found")
    return None


if __name__ == "__main__":
    perf_binary = select_perf_binary()
    print(f"Selected perf: {perf_binary}")
