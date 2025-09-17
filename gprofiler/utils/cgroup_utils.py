#
# Copyright (C) 2025 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import logging
from pathlib import Path
from typing import List, Optional, Tuple, Dict
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class CgroupResourceUsage:
    """Represents resource usage for a cgroup"""
    cgroup_path: str
    name: str
    cpu_usage: int  # CPU usage in nanoseconds
    memory_usage: int  # Memory usage in bytes
    
    @property
    def total_score(self) -> float:
        """Calculate a combined score for ranking cgroups by resource usage"""
        # Normalize CPU (ns) and memory (bytes) to comparable scales
        # CPU: convert nanoseconds to seconds, then scale
        # Memory: convert bytes to MB, then scale
        cpu_score = self.cpu_usage / 1_000_000_000  # ns to seconds
        memory_score = self.memory_usage / (1024 * 1024)  # bytes to MB
        
        # Weight CPU and memory equally, but you could adjust these weights
        return cpu_score + memory_score


def is_cgroup_available() -> bool:
    """Check if cgroup filesystem is available and mounted"""
    return os.path.exists("/sys/fs/cgroup")


def get_cgroup_cpu_usage(cgroup_path: str) -> Optional[int]:
    """Get CPU usage for a cgroup in nanoseconds"""
    usage_file = os.path.join(cgroup_path, "cpuacct.usage")
    if not os.path.exists(usage_file):
        # Try alternative path
        alt_path = cgroup_path.replace("/cpu,cpuacct/", "/cpuacct/")
        usage_file = os.path.join(alt_path, "cpuacct.usage")
        if not os.path.exists(usage_file):
            return None
    
    try:
        with open(usage_file, 'r') as f:
            return int(f.read().strip())
    except (IOError, ValueError) as e:
        logger.debug(f"Failed to read CPU usage from {usage_file}: {e}")
        return None


def get_cgroup_memory_usage(cgroup_path: str) -> Optional[int]:
    """Get memory usage for a cgroup in bytes"""
    usage_file = os.path.join(cgroup_path, "memory.usage_in_bytes")
    if not os.path.exists(usage_file):
        return None
    
    try:
        with open(usage_file, 'r') as f:
            return int(f.read().strip())
    except (IOError, ValueError) as e:
        logger.debug(f"Failed to read memory usage from {usage_file}: {e}")
        return None


def find_all_cgroups() -> List[str]:
    """Find all available cgroups in the system"""
    cgroups = []
    
    # Common cgroup mount points to check
    cgroup_bases = [
        "/sys/fs/cgroup/cpu,cpuacct",
        "/sys/fs/cgroup/memory",
        "/sys/fs/cgroup/cpuacct",
    ]
    
    for base in cgroup_bases:
        if os.path.exists(base):
            try:
                # Walk through all subdirectories
                for root, dirs, files in os.walk(base):
                    # Skip the base directory itself
                    if root == base:
                        continue
                    
                    # Check if this directory has the necessary files
                    cpu_file = os.path.join(root, "cpuacct.usage")
                    memory_file = root.replace("/cpu,cpuacct/", "/memory/") + "/memory.usage_in_bytes"
                    
                    if os.path.exists(cpu_file) or os.path.exists(memory_file):
                        cgroups.append(root)
            except OSError as e:
                logger.debug(f"Error walking cgroup directory {base}: {e}")
                continue
    
    return list(set(cgroups))  # Remove duplicates


def get_cgroup_resource_usage(cgroup_path: str) -> Optional[CgroupResourceUsage]:
    """Get resource usage for a single cgroup"""
    cpu_usage = get_cgroup_cpu_usage(cgroup_path)
    
    # For memory, try to find the corresponding memory cgroup path
    memory_path = cgroup_path.replace("/cpu,cpuacct/", "/memory/")
    if not os.path.exists(memory_path):
        memory_path = cgroup_path.replace("/cpuacct/", "/memory/")
    
    memory_usage = get_cgroup_memory_usage(memory_path)
    
    # If we can't get any usage data, skip this cgroup
    if cpu_usage is None and memory_usage is None:
        return None
    
    # Use 0 as default if one metric is missing
    cpu_usage = cpu_usage or 0
    memory_usage = memory_usage or 0
    
    # Extract a readable name from the path
    name = os.path.basename(cgroup_path)
    if len(name) > 12:  # Truncate long container IDs
        name = name[:12]
    
    return CgroupResourceUsage(
        cgroup_path=cgroup_path,
        name=name,
        cpu_usage=cpu_usage,
        memory_usage=memory_usage
    )


def get_top_cgroups_by_usage(limit: int = 50) -> List[CgroupResourceUsage]:
    """Get the top N cgroups by resource usage"""
    if not is_cgroup_available():
        logger.warning("Cgroup filesystem not available")
        return []
    
    all_cgroups = find_all_cgroups()
    logger.debug(f"Found {len(all_cgroups)} cgroups to analyze")
    
    cgroup_usages = []
    for cgroup_path in all_cgroups:
        usage = get_cgroup_resource_usage(cgroup_path)
        if usage:
            cgroup_usages.append(usage)
    
    # Sort by total resource usage score (descending)
    cgroup_usages.sort(key=lambda x: x.total_score, reverse=True)
    
    logger.debug(f"Analyzed {len(cgroup_usages)} cgroups with resource data")
    
    return cgroup_usages[:limit]


def cgroup_to_perf_name(cgroup_path: str) -> str:
    """Convert a cgroup path to the name format expected by perf -G option"""
    # perf expects the cgroup name relative to the cgroup mount point
    # For example: /sys/fs/cgroup/memory/docker/abc123 -> docker/abc123
    
    # Find the relative path from the cgroup mount point
    for base in ["/sys/fs/cgroup/memory/", "/sys/fs/cgroup/cpu,cpuacct/", "/sys/fs/cgroup/cpuacct/"]:
        if cgroup_path.startswith(base):
            return cgroup_path[len(base):]
    
    # Fallback: just use the basename
    return os.path.basename(cgroup_path)


def get_top_cgroup_names_for_perf(limit: int = 50) -> List[str]:
    """Get top cgroup names in the format needed for perf -G option"""
    top_cgroups = get_top_cgroups_by_usage(limit)
    return [cgroup_to_perf_name(cgroup.cgroup_path) for cgroup in top_cgroups]


def validate_perf_cgroup_support() -> bool:
    """Check if the current perf binary supports cgroup filtering"""
    try:
        import subprocess
        result = subprocess.run(
            ["perf", "record", "--help"],
            capture_output=True,
            text=True,
            timeout=10
        )
        return "--cgroup" in result.stdout or "-G" in result.stdout
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
        return False
