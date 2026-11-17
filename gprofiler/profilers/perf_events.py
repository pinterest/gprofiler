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
PMU (Performance Monitoring Unit) event constants and detection.

This module provides:
- Hardware PMU event constants
- Event detection via 'perf list pmu'
- Translation of generic event names to architecture-specific PMU event names
- Event validation and normalization
"""

import logging
import platform
import subprocess
from typing import Dict, List, Set

logger = logging.getLogger(__name__)

# =============================================================================
# GLOBAL CONSTANTS
# =============================================================================

# Hardware PMU events that gprofiler supports
# These are standard perf hardware events available on most CPUs
SUPPORTED_PMU_EVENTS = [
    "cycles",                   # CPU cycles (default)
    "cpu-cycles",              # Alias for cycles
    "instructions",            # Instructions executed
    "cache-misses",            # L3 cache misses
    "cache-references",        # L3 cache accesses
    "branch-misses",           # Mispredicted branches
    "branch-instructions",     # Branch instructions
    "stalled-cycles-frontend", # Frontend stall cycles
    "stalled-cycles-backend",  # Backend stall cycles (not supported on all CPUs)
]

# Event name aliases for normalization
# Some events have multiple names that map to the same counter
PMU_EVENT_ALIASES = {
    "cpu-cycles": "cycles",     # Normalize to canonical name
}

# Generic events without their aliases
CANONICAL_PMU_EVENTS = [event for event in SUPPORTED_PMU_EVENTS if event not in PMU_EVENT_ALIASES]

# Architecture-specific PMU event names for each generic event, keyed by platform.machine().
# Used when 'perf list pmu' doesn't report the generic names, e.g. the armv8_pmuv3 driver only
# exposes its own event names (cpu_cycles, inst_retired, ...).
# Candidates are in order of preference and follow the kernel's generic event mapping
# (drivers/perf/arm_pmuv3.c), so counts are identical to those of the generic event.
# Note that on aarch64 the cache events count L1 data cache activity, not last-level cache.
ARCH_PMU_EVENT_TRANSLATIONS: Dict[str, Dict[str, List[str]]] = {
    "aarch64": {
        "cycles": ["cpu_cycles"],
        "instructions": ["inst_retired"],
        "cache-references": ["l1d_cache"],
        "cache-misses": ["l1d_cache_refill"],
        "branch-instructions": ["br_retired", "pc_write_retired"],
        "branch-misses": ["br_mis_pred", "br_mis_pred_retired"],
        "stalled-cycles-frontend": ["stall_frontend"],
        "stalled-cycles-backend": ["stall_backend"],
    },
}

# Default event when none specified or all filtered out
DEFAULT_PMU_EVENT = "cycles"

# Timeout for perf command execution (seconds)
PERF_DETECTION_TIMEOUT = 5


# =============================================================================
# PMU EVENT DETECTION
# =============================================================================

def detect_supported_pmu_events(perf_path: str) -> List[str]:
    """
    Detect PMU events supported by the host CPU.
    
    Args:
        perf_path: Full path to perf executable
        
    Returns:
        Sorted list of supported generic event names, e.g. ["cycles", "instructions"]
        Returns [DEFAULT_PMU_EVENT] if detection fails
        
    Example:
        >>> detect_supported_pmu_events("/usr/bin/perf")
        ['branch-instructions', 'cache-misses', 'cycles', 'instructions']
    """
    return sorted(detect_pmu_event_translations(perf_path))


def detect_pmu_event_translations(perf_path: str) -> Dict[str, str]:
    """
    Detect PMU events supported by the host CPU, along with the event name
    to pass to 'perf record -e' for each of them.

    Uses 'perf list pmu' to query hardware capabilities. A generic event is
    supported if perf lists its generic name (e.g. x86), or one of its
    architecture-specific names from ARCH_PMU_EVENT_TRANSLATIONS (e.g. aarch64).

    Args:
        perf_path: Full path to perf executable
        
    Returns:
        Mapping of supported generic event name -> perf event name
        Returns {DEFAULT_PMU_EVENT: DEFAULT_PMU_EVENT} if detection fails
        
    Example (aarch64):
        >>> detect_pmu_event_translations("/usr/bin/perf")
        {'cycles': 'cpu_cycles', 'instructions': 'inst_retired', ...}
    """
    default = {DEFAULT_PMU_EVENT: DEFAULT_PMU_EVENT}
    try:
        logger.info("Detecting supported PMU events on this host...")
        
        available_events = _query_perf_hardware_events(perf_path)
        translations = _resolve_event_translations(available_events, platform.machine())
        
        if translations:
            logger.info(
                f"Detected {len(translations)} supported PMU events: {sorted(translations)}"
            )
            translated = {event: name for event, name in translations.items() if event != name}
            if translated:
                logger.info(f"PMU events translated to architecture-specific events: {translated}")
            return translations
        else:
            logger.warning(
                f"No PMU events detected, using default: "
                f"[{DEFAULT_PMU_EVENT}]"
            )
            return default
            
    except subprocess.TimeoutExpired:
        logger.warning(
            f"Timeout detecting PMU events (>{PERF_DETECTION_TIMEOUT}s), "
            f"using default: [{DEFAULT_PMU_EVENT}]"
        )
        return default
        
    except Exception as e:
        logger.warning(
            f"Error detecting PMU events: {e}, "
            f"using default: [{DEFAULT_PMU_EVENT}]"
        )
        return default


def _query_perf_hardware_events(perf_path: str) -> Set[str]:
    """
    Run 'perf list pmu' and parse output for hardware event names.
    
    Args:
        perf_path: Full path to perf executable
        
    Returns:
        Set of raw event names from perf output
    """
    result = subprocess.run(
        [perf_path, "list", "pmu"],
        capture_output=True,
        text=True,
        timeout=PERF_DETECTION_TIMEOUT
    )
    
    if result.returncode != 0:
        logger.warning(f"'perf list pmu' failed: {result.stderr}")
        return set()
    
    events = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        
        # Skip empty lines and headers
        if not line or line.startswith("List of"):
            continue
        
        # Event lines format:
        # "  branch-instructions OR branches    [Hardware event]"
        # "  cache-misses                       [Hardware cache event]"
        # "  cpu_cycles OR armv8_pmuv3_0/cpu_cycles/   [Kernel PMU event]"
        parts = line.split()
        if not parts:
            continue
        
        # Extract primary event name (first word)
        event_name = parts[0]
        events.add(event_name)
        
        # Extract alias if present (after "OR")
        if len(parts) >= 3 and parts[1].lower() == "or":
            alias_name = parts[2]
            events.add(alias_name)
    
    return events


def _resolve_event_translations(available_events: Set[str], machine: str) -> Dict[str, str]:
    """
    Map each generic event supported by gprofiler to the perf event name
    to use on this host.

    The generic name (or one of its aliases) takes precedence, so hosts whose
    perf lists the generic names keep using them unchanged.
    
    Args:
        available_events: Set of event names from 'perf list pmu'
        machine: Host architecture, as returned by platform.machine()
        
    Returns:
        Mapping of supported generic event name -> perf event name

    Example:
        >>> _resolve_event_translations({"cpu-cycles", "instructions"}, "x86_64")
        {"cycles": "cycles", "instructions": "instructions"}
        >>> _resolve_event_translations({"cpu_cycles", "inst_retired"}, "aarch64")
        {"cycles": "cpu_cycles", "instructions": "inst_retired"}
    """
    arch_translations = ARCH_PMU_EVENT_TRANSLATIONS.get(machine, {})
    translations = {}
    for event in CANONICAL_PMU_EVENTS:
        generic_names = [event] + [alias for alias, canonical in PMU_EVENT_ALIASES.items() if canonical == event]
        if available_events.intersection(generic_names):
            translations[event] = event
            continue
        for arch_name in arch_translations.get(event, []):
            if arch_name in available_events:
                translations[event] = arch_name
                break
    return translations


def translate_events(events: List[str], translations: Dict[str, str]) -> List[str]:
    """
    Translate generic event names to the perf event names to use on this host.

    Events without a translation are passed through unchanged, leaving it to
    perf and the kernel to resolve them.
    
    Args:
        events: Normalized generic event names, e.g. ["cycles", "cache-misses"]
        translations: Mapping returned by detect_pmu_event_translations()
        
    Returns:
        List of perf event names, in the same order
        
    Example (aarch64):
        >>> translate_events(["cycles", "cache-misses"], {"cycles": "cpu_cycles", "cache-misses": "l1d_cache_refill"})
        ['cpu_cycles', 'l1d_cache_refill']
    """
    return [translations.get(event, event) for event in events]


# =============================================================================
# EVENT VALIDATION
# =============================================================================

def validate_and_normalize_events(events: List[str]) -> List[str]:
    """
    Validate and normalize a list of PMU event names.
    
    This function:
    1. Filters out unsupported events (not in SUPPORTED_PMU_EVENTS)
    2. Normalizes aliases (cpu-cycles -> cycles)
    3. Removes duplicates
    4. Returns at least [DEFAULT_PMU_EVENT] as fallback
    
    Args:
        events: List of event names to validate
        
    Returns:
        List of valid, normalized, deduplicated event names
        Returns [DEFAULT_PMU_EVENT] if input is empty or all invalid
        
    Example:
        >>> validate_and_normalize_events(
        ...     ["cpu-cycles", "invalid-event", "instructions", "cycles"]
        ... )
        ['cycles', 'instructions']
    """
    if not events:
        return [DEFAULT_PMU_EVENT]
    
    valid_events = []
    for event in events:
        # Skip unsupported events
        if event not in SUPPORTED_PMU_EVENTS:
            continue
        
        # Normalize using aliases
        normalized = PMU_EVENT_ALIASES.get(event, event)
        
        # Avoid duplicates
        if normalized not in valid_events:
            valid_events.append(normalized)
    
    # Fallback to default if all events were filtered out
    return valid_events if valid_events else [DEFAULT_PMU_EVENT]
