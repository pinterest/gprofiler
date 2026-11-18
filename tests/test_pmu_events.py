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
"""PMU event detection from 'perf list pmu' output, and translation of generic event names
to architecture-specific ones."""

import platform
import subprocess
from typing import Any

import pytest

from gprofiler.profilers import perf_events

# Excerpt of 'perf list pmu' on an AMD EPYC (Zen 4) host, perf 6.17
X86_PERF_LIST_PMU = """\
  branch-instructions OR cpu/branch-instructions/    [Kernel PMU event]
  branch-misses OR cpu/branch-misses/                [Kernel PMU event]
  cache-misses OR cpu/cache-misses/                  [Kernel PMU event]
  cache-references OR cpu/cache-references/          [Kernel PMU event]
  cpu-cycles OR cpu/cpu-cycles/                      [Kernel PMU event]
  instructions OR cpu/instructions/                  [Kernel PMU event]
  ref-cycles OR cpu/ref-cycles/                      [Kernel PMU event]
  stalled-cycles-frontend OR cpu/stalled-cycles-frontend/[Kernel PMU event]
  msr/aperf/                                         [Kernel PMU event]

branch:
  ex_ret_brn_misp
       [Retired branch instructions mispredicted. Unit: cpu]
"""

# Excerpt of 'perf list pmu' on a Graviton3 (Neoverse-V1) host, with the perf bundled in gprofiler (6.7)
ARM_PERF_LIST_PMU = """\
  br_mis_pred OR armv8_pmuv3_0/br_mis_pred/          [Kernel PMU event]
  br_mis_pred_retired OR armv8_pmuv3_0/br_mis_pred_retired/[Kernel PMU event]
  br_retired OR armv8_pmuv3_0/br_retired/            [Kernel PMU event]
  bus_cycles OR armv8_pmuv3_0/bus_cycles/            [Kernel PMU event]
  cpu_cycles OR armv8_pmuv3_0/cpu_cycles/            [Kernel PMU event]
  inst_retired OR armv8_pmuv3_0/inst_retired/        [Kernel PMU event]
  l1d_cache OR armv8_pmuv3_0/l1d_cache/              [Kernel PMU event]
  l1d_cache_refill OR armv8_pmuv3_0/l1d_cache_refill/[Kernel PMU event]
  ll_cache_miss_rd OR armv8_pmuv3_0/ll_cache_miss_rd/[Kernel PMU event]
  ll_cache_rd OR armv8_pmuv3_0/ll_cache_rd/          [Kernel PMU event]
  stall_backend OR armv8_pmuv3_0/stall_backend/      [Kernel PMU event]
  stall_frontend OR armv8_pmuv3_0/stall_frontend/    [Kernel PMU event]
"""

# Excerpt of 'perf list pmu' on the same Graviton3 host, with perf 6.8 built with vendor event tables
ARM_PERF_LIST_PMU_WITH_JEVENTS = """\
general:
  cnt_cycles
       [Constant frequency cycles. Unit: armv8_pmuv3_0]
  cpu_cycles
       [Cycle. Unit: armv8_pmuv3_0]

retired:
  br_mis_pred_retired
       [Instruction architecturally executed, mispredicted branch. Unit:
        armv8_pmuv3_0]
  inst_retired
       [Instruction architecturally executed. Unit: armv8_pmuv3_0]
"""

ARM_TRANSLATIONS = {
    "cycles": "cpu_cycles",
    "instructions": "inst_retired",
    "cache-references": "ll_cache_rd",
    "cache-misses": "ll_cache_miss_rd",
    "branch-instructions": "br_retired",
    "branch-misses": "br_mis_pred",
    "stalled-cycles-frontend": "stall_frontend",
    "stalled-cycles-backend": "stall_backend",
}


def _mock_perf_list(monkeypatch: pytest.MonkeyPatch, machine: str, stdout: str = "", returncode: int = 0) -> None:
    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(platform, "machine", lambda: machine)


def test_x86_uses_generic_names(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_perf_list(monkeypatch, "x86_64", X86_PERF_LIST_PMU)

    translations = perf_events.detect_pmu_event_translations("perf")

    assert translations == {
        "cycles": "cycles",
        "instructions": "instructions",
        "cache-references": "cache-references",
        "cache-misses": "cache-misses",
        "branch-instructions": "branch-instructions",
        "branch-misses": "branch-misses",
        "stalled-cycles-frontend": "stalled-cycles-frontend",
    }


def test_aarch64_translates_to_arch_names(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_perf_list(monkeypatch, "aarch64", ARM_PERF_LIST_PMU)

    assert perf_events.detect_pmu_event_translations("perf") == ARM_TRANSLATIONS
    assert perf_events.detect_supported_pmu_events("perf") == sorted(ARM_TRANSLATIONS)


def test_aarch64_cache_events_fall_back_to_l1(monkeypatch: pytest.MonkeyPatch) -> None:
    without_ll_cache = "".join(
        line for line in ARM_PERF_LIST_PMU.splitlines(keepends=True) if not line.strip().startswith("ll_cache")
    )
    _mock_perf_list(monkeypatch, "aarch64", without_ll_cache)

    translations = perf_events.detect_pmu_event_translations("perf")

    assert translations["cache-references"] == "l1d_cache"
    assert translations["cache-misses"] == "l1d_cache_refill"


def test_aarch64_parses_categorized_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_perf_list(monkeypatch, "aarch64", ARM_PERF_LIST_PMU_WITH_JEVENTS)

    assert perf_events.detect_pmu_event_translations("perf") == {
        "cycles": "cpu_cycles",
        "instructions": "inst_retired",
        "branch-misses": "br_mis_pred_retired",
    }


def test_arch_names_ignored_on_other_architectures(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_perf_list(monkeypatch, "x86_64", ARM_PERF_LIST_PMU)

    assert perf_events.detect_pmu_event_translations("perf") == {"cycles": "cycles"}


def test_detection_failure_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_perf_list(monkeypatch, "aarch64", returncode=1)

    assert perf_events.detect_pmu_event_translations("perf") == {"cycles": "cycles"}


def test_translate_events_passes_unknown_events_through() -> None:
    assert perf_events.translate_events(["cycles", "cache-misses", "branch-misses"], {"cycles": "cpu_cycles"}) == [
        "cpu_cycles",
        "cache-misses",
        "branch-misses",
    ]
