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
    _parse_trace_csv,
    _workload_env,
    find_nsys,
    generate_nsys_timeline_html,
    nsys_stats_to_collapsed,
    nsys_trace_to_timeline_events,
)


KERN_CSV = """Time (%),Total Time (ns),Instances,Avg (ns),Med (ns),Min (ns),Max (ns),StdDev (ns),Name
100.0,28918591,5,5783718.2,5780070.0,5777126,5797542,8313.1,"burn(float *, int, int)"
"""

API_CSV = """Time (%),Total Time (ns),Num Calls,Avg (ns),Med (ns),Min (ns),Max (ns),StdDev (ns),Name
81.2,131012864,1,131012864.0,131012864.0,131012864,131012864,0.0,cudaMalloc
17.9,28943134,5,5788626.8,5785451.0,5783190,5801941,7724.7,cudaDeviceSynchronize
"""

GPU_TRACE_CSV = (
    "Start (ns),Duration (ns),CorrId,GrdX,GrdY,GrdZ,BlkX,BlkY,BlkZ,Reg/Trd,"
    "StcSMem (MB),DymSMem (MB),Bytes (MB),Throughput (MBps),SrcMemKd,DstMemKd,Device,Ctx,Strm,Name\n"
    '1000,5000,101,160,1,1,256,1,1,32,0.000,0.000,,,,,NVIDIA A10G (0),1,7,"burn(float *, int, int)"\n'
    '9000,4000,102,160,1,1,256,1,1,32,0.000,0.000,,,,,NVIDIA A10G (0),1,7,"burn(float *, int, int)"\n'
)

API_TRACE_CSV = """Start (ns),Duration (ns),Name,Result,CorrID,Pid,Tid,T-Pri,Thread Name
500,300,cudaLaunchKernel,0,101,4242,4242,20,python3
8600,250,cudaLaunchKernel,0,102,4242,4242,20,python3
14000,2000,cudaDeviceSynchronize,0,103,4242,4242,20,python3
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


def test_workload_env_restores_original_ld_library_path(monkeypatch):
    # PyInstaller bundle sets LD_LIBRARY_PATH to its own libs and saves the
    # pre-launch value in *_ORIG. The spawned workload must get the original.
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxxxx")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/x86_64-linux-gnu")
    env = _workload_env()
    assert env["LD_LIBRARY_PATH"] == "/usr/lib/x86_64-linux-gnu"
    assert "LD_LIBRARY_PATH_ORIG" not in env


def test_workload_env_empty_orig_unsets_var(monkeypatch):
    # When *_ORIG is empty, the var was unset before the bundle ran: unset it.
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxxxx")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "")
    env = _workload_env()
    assert "LD_LIBRARY_PATH" not in env


def test_workload_env_noop_without_orig(monkeypatch):
    # Not running under PyInstaller (no *_ORIG, no _MEI): leave the env as-is.
    monkeypatch.setenv("LD_LIBRARY_PATH", "/keep/this")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    env = _workload_env()
    assert env["LD_LIBRARY_PATH"] == "/keep/this"


def test_workload_env_strips_mei_when_no_orig(monkeypatch):
    # No *_ORIG saved, but LD_LIBRARY_PATH carries a PyInstaller _MEI bundle dir:
    # drop the bundle path, keep the rest so the child finds system libs.
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIabc123:/usr/lib/x86_64-linux-gnu")
    env = _workload_env()
    assert env["LD_LIBRARY_PATH"] == "/usr/lib/x86_64-linux-gnu"


def test_workload_env_unsets_when_only_mei(monkeypatch):
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIabc123")
    env = _workload_env()
    assert "LD_LIBRARY_PATH" not in env


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


def test_parse_trace_csv_gpu():
    events = _parse_trace_csv(GPU_TRACE_CSV, "gpu")
    assert len(events) == 2
    first = events[0]
    assert first["start"] == 1000
    assert first["dur"] == 5000
    assert first["corr"] == 101
    assert first["name"] == "burn(float *, int, int)"
    assert first["lane"] == "GPU NVIDIA A10G (0) stream 7"


def test_parse_trace_csv_api():
    events = _parse_trace_csv(API_TRACE_CSV, "api")
    assert len(events) == 3
    launch = events[0]
    assert launch["name"] == "cudaLaunchKernel"
    assert launch["corr"] == 101
    assert launch["lane"] == "CPU pid 4242 tid 4242"


def test_parse_trace_csv_missing_columns():
    assert _parse_trace_csv("Foo,Bar\n1,2\n", "gpu") == []
    assert _parse_trace_csv("", "api") == []


def test_generate_timeline_html_links_corrid():
    events = {
        "gpu": _parse_trace_csv(GPU_TRACE_CSV, "gpu"),
        "api": _parse_trace_csv(API_TRACE_CSV, "api"),
    }
    html = generate_nsys_timeline_html(events)
    assert html is not None
    assert html.startswith("<!DOCTYPE html>")
    # CPU lanes listed before GPU lanes; both present
    assert "CPU pid 4242 tid 4242" in html
    assert "GPU NVIDIA A10G (0) stream 7" in html
    # kernel + API names present exactly once each (name table dedup)
    assert html.count("burn(float *, int, int)") == 1
    assert html.count("cudaLaunchKernel") == 1
    # correlation ids embedded for CPU<->GPU linking
    assert "101" in html and "102" in html
    # no external assets — must render standalone in the Studio iframe
    assert "http://" not in html and "https://" not in html
    # auto-zoom on load + density shading for sub-pixel events
    assert "initView()" in html
    assert "Full span" in html
    assert "density" in html


def test_generate_timeline_html_empty():
    assert generate_nsys_timeline_html({"gpu": [], "api": []}) is None


def test_nsys_trace_to_timeline_events(tmp_path: Path, monkeypatch):
    nsys = tmp_path / "nsys"
    nsys.write_text("#!/bin/sh\n")
    nsys.chmod(0o755)
    rep = tmp_path / "cap.nsys-rep"
    rep.write_bytes(b"fake")

    def fake_run(cmd, **kwargs):
        out_prefix = Path(cmd[cmd.index("-o") + 1])
        report = next(a for a in cmd if a.startswith("--report=")).split("=", 1)[1]
        csv_text = GPU_TRACE_CSV if report == "cuda_gpu_trace" else API_TRACE_CSV
        Path(str(out_prefix) + f"_{report}.csv").write_text(csv_text)
        return mock.Mock(returncode=0, stdout=b"ok")

    monkeypatch.setattr("gprofiler.nsys_profiler.subprocess.run", fake_run)
    events = nsys_trace_to_timeline_events(nsys, rep, tmp_path / "work")
    assert events is not None
    assert len(events["gpu"]) == 2
    assert len(events["api"]) == 3


def test_nsys_trace_to_timeline_events_none_when_empty(tmp_path: Path, monkeypatch):
    nsys = tmp_path / "nsys"
    nsys.write_text("#!/bin/sh\n")
    nsys.chmod(0o755)
    rep = tmp_path / "cap.nsys-rep"
    rep.write_bytes(b"fake")

    def fake_run(cmd, **kwargs):
        return mock.Mock(returncode=1, stdout=b"no data")

    monkeypatch.setattr("gprofiler.nsys_profiler.subprocess.run", fake_run)
    assert nsys_trace_to_timeline_events(nsys, rep, tmp_path / "work") is None
