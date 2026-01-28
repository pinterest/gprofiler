#
# Copyright (C) 2026 Intel Corporation
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

"""
PMU (Performance Monitoring Unit) event constants for perf profiler.
"""

from typing import List

# Supported PMU events for perf profiler
# Note: stalled-cycles-backend removed as it's not supported on many CPUs (e.g., AMD)
# TODO: next PR it will be dynamically detected based on the host capabilities
SUPPORTED_PMU_EVENTS = [
    "cycles",
    "cpu-cycles",           # Alias for "cycles"
    "instructions",
    "cache-misses",
    "cache-references",
    "branch-misses",
    "branch-instructions",
    "stalled-cycles-frontend",
]

# Event aliases for normalization
PMU_EVENT_ALIASES = {
    "cpu-cycles": "cycles",  # Normalize cpu-cycles to cycles
}


def validate_and_normalize_events(events: List[str]) -> List[str]:
    """
    Validate and normalize PMU events.
    
    - Filters out unsupported events
    - Normalizes aliases (cpu-cycles -> cycles)
    - Returns at least ["cycles"] as fallback
    """
    if not events:
        return ["cycles"]
    
    valid_events = []
    for event in events:
        if event in SUPPORTED_PMU_EVENTS:
            # Normalize using aliases
            normalized = PMU_EVENT_ALIASES.get(event, event)
            if normalized not in valid_events:  # Avoid duplicates
                valid_events.append(normalized)
    
    # Fallback to cycles if no valid events
    return valid_events if valid_events else ["cycles"]
