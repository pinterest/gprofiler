#
# Copyright (C) 2026 Intel Corporation / Pinterest
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
"""Unit tests for gprofiler.nsys_profiler (no GPU / nsys required)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from gprofiler.nsys_profiler import (
    _csv_to_collapsed,
    find_nsys,
    nsys_stats_to_collapsed,
)


KERN_CSV = """Time (%),Total Time (ns),Instances,Avg (ns),Med (ns),Min (ns),Max (ns),StdDev (ns),Name
100.0,28918591,5,5783718.2,5780070.0,5777126,5797542,8313.1,"burn(float *, int, int)"
"""

API_CSV = """Time (%),Total Time (ns),Num Calls,Avg (ns),Med (ns),Min (ns),Max (ns),StdDev (ns),Name
81.2,131012864,1,131012864.0,131012864.0,131012864,131012864,0.0,cudaMalloc
17.9,28943134,5,5788626.8,5785451.0,5783190,5801941,7724.7,cudaDeviceSynchronize
"""


def test_csv_to_collapsed_kern():
    collapsed = _csv_to_collapsed(KERN_CSV, "gpu;nsys;cuda_kernel")
    assert collapsed is not None
    assert "gpu;nsys;cuda_kernel;burn(float *, int, int)" in collapsed
    # weight = 28918591 // 1000
    assert collapsed.split()[-1] == "28918"


def test_csv_to_collapsed_api():
    collapsed = _csv_to_collapsed(API_CSV, "gpu;nsys;cuda_api")
    assert collapsed is not None
    lines = collapsed.splitlines()
    assert len(lines) == 2
    assert "cudaMalloc" in lines[0]
    assert "cudaDeviceSynchronize" in lines[1]


def test_find_nsys_explicit(tmp_path: Path):
    fake = tmp_path / "nsys"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    found = find_nsys(str(fake))
    assert found is not None
    assert found.name == "nsys"


def test_find_nsys_missing_falls_through_to_path(monkeypatch, tmp_path: Path):
    # Invalid explicit path should not block discovery of a real nsys on PATH.
    monkeypatch.setenv("PATH", str(tmp_path))  # empty PATH dir
    monkeypatch.delenv("NSYS_PATH", raising=False)
    # With no PATH hit and a nonsense explicit path, still may find /opt installs —
    # assert only that an explicitly *valid* missing path doesn't crash.
    result = find_nsys("/nonexistent/nsys-binary-xyz")
    # Either None (no system nsys) or a real install — never raises.
    assert result is None or result.name == "nsys"


def test_find_nsys_prefers_explicit(tmp_path: Path):
    fake = tmp_path / "nsys"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    found = find_nsys(str(fake))
    assert found is not None
    assert found == fake.resolve()


def test_nsys_stats_to_collapsed_uses_kern(tmp_path: Path, monkeypatch):
    nsys = tmp_path / "nsys"
    nsys.write_text("#!/bin/sh\n")
    nsys.chmod(0o755)
    rep = tmp_path / "cap.nsys-rep"
    rep.write_bytes(b"fake")

    def fake_run(cmd, **kwargs):
        # Write kern CSV where nsys_stats_to_collapsed looks
        out_prefix = Path(cmd[cmd.index("-o") + 1])
        csv_path = Path(str(out_prefix) + "_cuda_gpu_kern_sum.csv")
        csv_path.write_text(KERN_CSV)
        return mock.Mock(returncode=0, stdout=b"ok")

    monkeypatch.setattr("gprofiler.nsys_profiler.subprocess.run", fake_run)
    collapsed = nsys_stats_to_collapsed(nsys, rep, tmp_path / "work")
    assert collapsed is not None
    assert "cuda_kernel;burn" in collapsed
