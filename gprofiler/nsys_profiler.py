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
"""NVIDIA Nsight Systems (nsys) GPU capture helpers for adhoc flamegraphs.

Host-detect only — nsys is NOT bundled with the agent (same packaging model as
PerfSpect). See docs/NSYS_GPU_PROFILING.md.
"""

from __future__ import annotations

import csv
import glob
import io
import logging
import os
import shlex
import shutil
import subprocess  # nosec B404
from pathlib import Path
from typing import Callable, List, Optional, Sequence

logger = logging.getLogger(__name__)

NSYS_DATA_DIRECTORY = "/tmp/gprofiler_nsys"
DEFAULT_NSYS_CANDIDATES = (
    "/usr/local/bin/nsys",
    "/opt/nvidia/nsight-systems/2025.6.1/bin/nsys",
    "/opt/nvidia/nsight-systems/2025.5.1/bin/nsys",
    "/opt/nvidia/nsight-systems/2024.6.2/bin/nsys",
)


def _workload_env() -> dict:
    """Environment for the nsys-wrapped workload.

    gProfiler runs as a PyInstaller bundle that prepends its own lib dir
    (/tmp/_MEIxxxx) to LD_LIBRARY_PATH; a spawned workload (e.g. dynamically-linked
    PyTorch) would otherwise pick up the bundle's older libstdc++ and fail to
    import (CXXABI_1.3.8 not found). Prefer PyInstaller's saved *_ORIG value; if it
    is absent, strip any _MEI bundle path from LD_LIBRARY_PATH / LD_PRELOAD so the
    child resolves the system libraries.
    """
    env = os.environ.copy()
    for var in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        orig = env.pop(f"{var}_ORIG", None)
        if orig is not None:
            if orig:
                env[var] = orig
            else:
                env.pop(var, None)
            continue
        current = env.get(var)
        if current:
            cleaned = os.pathsep.join(p for p in current.split(os.pathsep) if p and "/_MEI" not in p)
            if cleaned:
                env[var] = cleaned
            else:
                env.pop(var, None)
    return env


def find_nsys(explicit_path: Optional[str] = None) -> Optional[Path]:
    """Locate an executable nsys binary.

    Search order: explicit_path → NSYS_PATH env → PATH → common install paths.
    """
    candidates: List[str] = []
    if explicit_path:
        candidates.append(explicit_path)
    env_path = os.environ.get("NSYS_PATH")
    if env_path:
        candidates.append(env_path)
    which = shutil.which("nsys")
    if which:
        candidates.append(which)
    candidates.extend(DEFAULT_NSYS_CANDIDATES)
    # Glob any versioned install. Some packages ship only the target-linux-* tree
    # without the bin/ symlink, so check both layouts.
    candidates.extend(sorted(glob.glob("/opt/nvidia/nsight-systems/*/bin/nsys"), reverse=True))
    candidates.extend(sorted(glob.glob("/opt/nvidia/nsight-systems/*/target-linux-*/nsys"), reverse=True))

    seen = set()
    for raw in candidates:
        if not raw or raw in seen:
            continue
        seen.add(raw)
        path = Path(raw)
        try:
            if path.is_file() and os.access(path, os.X_OK):
                # Resolve symlinks for logging, but return the usable path.
                return path.resolve() if path.exists() else path
        except OSError:
            continue
    return None


def _parse_workload(workload: Optional[str | Sequence[str]]) -> Optional[List[str]]:
    if workload is None or workload == "":
        return None
    if isinstance(workload, (list, tuple)):
        return [str(x) for x in workload]
    return shlex.split(str(workload))


def run_nsys_capture(
    nsys: Path,
    output_prefix: Path,
    duration_sec: int,
    workload_cmd: Optional[Sequence[str]] = None,
    stop_event=None,
) -> Optional[Path]:
    """Run `nsys profile` and return the path to the `.nsys-rep` file, or None."""
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    duration_sec = max(1, int(duration_sec))

    cmd: List[str] = [
        str(nsys),
        "profile",
        "-o",
        str(output_prefix),
        "--force-overwrite=true",
        # CUDA-focused, skip heavy CPU sampling / slow symbol waits where possible.
        "-t",
        "cuda,nvtx",
        "-s",
        "none",
    ]

    workload = list(workload_cmd) if workload_cmd else None
    if workload:
        cmd.extend(workload)
    else:
        # Duration-bounded no-op session: sleep so nsys still produces a report.
        # Prefer a real CUDA workload via nsys_workload for useful kern sums.
        cmd.extend(["--duration", str(duration_sec), "sleep", str(duration_sec)])

    logger.info("Running nsys capture: %s", " ".join(cmd))
    timeout = duration_sec + 120
    try:
        # nosec B603 — nsys path validated; workload from operator config
        proc = subprocess.run(  # nosec B603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            env=_workload_env(),
        )
        if proc.stdout:
            logger.info("nsys profile output (tail): %s", proc.stdout.decode("utf-8", "replace")[-2000:])
        if proc.returncode != 0:
            logger.error("nsys profile exited with code %s", proc.returncode)
            return None
    except subprocess.TimeoutExpired:
        logger.error("nsys profile timed out after %ss", timeout)
        return None
    except OSError as exc:
        logger.error("Failed to execute nsys: %s", exc)
        return None

    if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
        logger.info("Stop event set during nsys capture; discarding result")
        return None

    rep = Path(str(output_prefix) + ".nsys-rep")
    if not rep.is_file():
        # Some nsys versions may write without forcing the suffix identically.
        matches = list(output_prefix.parent.glob(output_prefix.name + "*.nsys-rep"))
        if matches:
            rep = matches[0]
    if not rep.is_file():
        logger.error("nsys profile completed but .nsys-rep not found at %s", rep)
        return None
    return rep


def _csv_to_collapsed(csv_text: str, frame_prefix: str) -> Optional[str]:
    """Convert an nsys stats CSV into collapsed stacks.

    Uses Total Time (ns) (or the first numeric time-like column) as weight.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return None

    # Normalize header keys (strip BOM / whitespace)
    fieldnames = [f.strip().lstrip("\ufeff") for f in reader.fieldnames]
    reader.fieldnames = fieldnames

    name_key = next((f for f in fieldnames if f.lower() in ("name", "kernel name", "kernel")), None)
    time_key = next(
        (
            f
            for f in fieldnames
            if f.lower() in ("total time (ns)", "total time(ns)", "total (ns)", "time (ns)")
        ),
        None,
    )
    if name_key is None:
        # Fall back to last column as name (nsys puts Name last)
        name_key = fieldnames[-1]
    if time_key is None:
        # Prefer any column containing 'total' and 'ns'
        time_key = next(
            (f for f in fieldnames if "total" in f.lower() and "ns" in f.lower()),
            None,
        )
    if time_key is None:
        time_key = next((f for f in fieldnames if "time" in f.lower()), None)

    lines: List[str] = []
    for row in reader:
        # DictReader may still use original keys; build a stripped map.
        cleaned = { (k or "").strip().lstrip("\ufeff"): (v or "").strip() for k, v in row.items() }
        name = cleaned.get(name_key, "").strip().strip('"')
        if not name:
            continue
        raw_time = cleaned.get(time_key or "", "0") or "0"
        try:
            # Total Time may be float ns
            weight = max(1, int(float(raw_time)))
        except ValueError:
            weight = 1
        # Scale down huge ns values to keep flamegraph samples manageable
        # (1 sample ≈ 1µs of GPU time). Keep at least 1.
        samples = max(1, weight // 1000)
        safe_name = name.replace(";", ",")
        lines.append(f"{frame_prefix};{safe_name} {samples}")

    return "\n".join(lines) if lines else None


def nsys_stats_to_collapsed(nsys: Path, nsys_rep: Path, work_dir: Path) -> Optional[str]:
    """Export cuda_gpu_kern_sum (fallback cuda_api_sum) and convert to collapsed stacks."""
    work_dir.mkdir(parents=True, exist_ok=True)

    def _export(report: str, out_prefix: Path) -> Optional[str]:
        cmd = [
            str(nsys),
            "stats",
            f"--report={report}",
            "--format=csv",
            "--force-export=true",
            "-o",
            str(out_prefix),
            str(nsys_rep),
        ]
        try:
            proc = subprocess.run(  # nosec B603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
                check=False,
            )
            if proc.returncode != 0:
                logger.warning(
                    "nsys stats %s failed (rc=%s): %s",
                    report,
                    proc.returncode,
                    (proc.stdout or b"")[-1000:].decode("utf-8", "replace"),
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("nsys stats %s error: %s", report, exc)
            return None

        matches = sorted(work_dir.glob(out_prefix.name + "*.csv"))
        # Also check CWD-relative names nsys sometimes writes
        matches += sorted(Path(".").glob(out_prefix.name + "*.csv"))
        matches += sorted(nsys_rep.parent.glob(out_prefix.name + "*.csv"))
        # Dedup
        seen = set()
        unique = []
        for m in matches:
            rp = str(m.resolve()) if m.exists() else str(m)
            if rp not in seen and m.is_file() and m.stat().st_size > 0:
                seen.add(rp)
                unique.append(m)
        if not unique:
            logger.info("nsys stats %s produced no non-empty CSV", report)
            return None
        return unique[0].read_text(encoding="utf-8", errors="replace")

    kern_csv = _export("cuda_gpu_kern_sum", work_dir / "kern")
    if kern_csv:
        collapsed = _csv_to_collapsed(kern_csv, "gpu;nsys;cuda_kernel")
        if collapsed:
            logger.info("Built collapsed stacks from cuda_gpu_kern_sum (%d lines)", collapsed.count("\n") + 1)
            return collapsed

    api_csv = _export("cuda_api_sum", work_dir / "api")
    if api_csv:
        collapsed = _csv_to_collapsed(api_csv, "gpu;nsys;cuda_api")
        if collapsed:
            logger.info("Built collapsed stacks from cuda_api_sum fallback (%d lines)", collapsed.count("\n") + 1)
            return collapsed

    logger.error("No usable nsys stats CSV (cuda_gpu_kern_sum / cuda_api_sum)")
    return None


def _simple_gpu_flamegraph_html(collapsed: str, title: str = "nsys GPU profile") -> str:
    """Minimal self-contained HTML for Adhoc iframe when burn/template unavailable."""
    rows = []
    total = 0
    parsed = []
    for line in collapsed.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            stack, count_s = line.rsplit(" ", 1)
            count = int(count_s)
        except ValueError:
            continue
        parsed.append((stack, count))
        total += count
    total = total or 1
    # Sort widest first (flamegraph convention)
    parsed.sort(key=lambda x: -x[1])
    bars = []
    for stack, count in parsed:
        pct = 100.0 * count / total
        leaf = stack.split(";")[-1]
        bars.append(
            f'<div class="bar" style="width:{pct:.2f}%" title="{stack} ({count})">'
            f"<span>{leaf}</span><em>{pct:.1f}%</em></div>"
        )
    bars_html = "\n".join(bars) if bars else "<p>No GPU samples</p>"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 24px; background: #0b1020; color: #e8ecf5; }}
h1 {{ font-size: 1.1rem; font-weight: 600; }}
.meta {{ color: #9aa3b5; font-size: 0.85rem; margin-bottom: 16px; }}
.frame {{ background: #151b2e; border-radius: 8px; padding: 12px; }}
.bar {{ background: linear-gradient(90deg,#c45c26,#e6a15c); color: #1a1208;
  margin: 4px 0; padding: 6px 10px; border-radius: 4px; box-sizing: border-box;
  min-width: 2px; display: flex; justify-content: space-between; gap: 8px;
  font-size: 0.8rem; overflow: hidden; white-space: nowrap; }}
.bar em {{ font-style: normal; opacity: 0.85; }}
</style></head><body>
<h1>{title}</h1>
<p class="meta">Collapsed stacks from NVIDIA nsys (cuda_gpu_kern_sum / cuda_api_sum).
Total weight: {total}. Inspired by GPU flame-graph views; host-detect nsys (not bundled).</p>
<div class="frame">{bars_html}</div>
</body></html>
"""


def generate_nsys_flamegraph_html(
    collapsed: str,
    start_iso: str,
    end_iso: str,
    generate_html_fn: Optional[Callable[..., Optional[str]]] = None,
) -> Optional[str]:
    """Turn collapsed stacks into flamegraph HTML via callback or simple fallback."""
    if generate_html_fn is not None:
        try:
            html = generate_html_fn(collapsed)
            if html:
                return html
        except Exception as exc:
            logger.warning("generate_html_fn failed (%s); using simple GPU HTML fallback", exc)

    # Prefer burn+template when the full agent runtime is available.
    try:
        from gprofiler.utils import resource_path, run_process, get_iso8601_format_time
        from datetime import datetime as _dt

        start = _dt.fromisoformat(start_iso.replace("Z", "+00:00")) if start_iso else _dt.utcnow()
        end = _dt.fromisoformat(end_iso.replace("Z", "+00:00")) if end_iso else _dt.utcnow()

        start_ts = get_iso8601_format_time(start)
        end_ts = get_iso8601_format_time(end)
        html = (
            Path(resource_path("flamegraph/flamegraph_template.html"))
            .read_bytes()
            .replace(
                b"{{{JSON_DATA}}}",
                run_process(
                    [resource_path("burn"), "convert", "--type=folded"],
                    suppress_log=True,
                    stdin=collapsed.encode(),
                    stop_event=None,
                    timeout=30,
                ).stdout,
            )
            .replace(b"{{{START_TIME}}}", start_ts.encode())
            .replace(b"{{{END_TIME}}}", end_ts.encode())
        )
        return html.decode("utf-8")
    except Exception as exc:
        logger.info("burn/template HTML unavailable (%s); using simple GPU HTML fallback", exc)
        return _simple_gpu_flamegraph_html(collapsed)


def collect_nsys_adhoc_html(
    *,
    duration_sec: int,
    nsys_path: Optional[str] = None,
    workload: Optional[str | Sequence[str]] = None,
    work_dir: Optional[str] = None,
    stop_event=None,
    generate_html_fn: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """End-to-end: find nsys → capture → collapsed → HTML. Returns HTML or None."""
    nsys = find_nsys(nsys_path)
    if nsys is None:
        logger.error(
            "enable_nsys set but nsys not found (install Nsight Systems on the host "
            "or set NSYS_PATH / --nsys-path). nsys is not bundled with gProfiler."
        )
        return None

    logger.info("Using nsys at %s", nsys)
    base = Path(work_dir or NSYS_DATA_DIRECTORY)
    base.mkdir(parents=True, exist_ok=True)
    prefix = base / "gprofiler_nsys_capture"
    workload_cmd = _parse_workload(workload)

    if workload_cmd is None:
        logger.warning(
            "No nsys_workload configured; capturing a duration-bounded sleep session. "
            "Set nsys_workload (e.g. path to cuda_burn) for real CUDA kernel frames."
        )

    rep = run_nsys_capture(
        nsys,
        prefix,
        duration_sec=duration_sec,
        workload_cmd=workload_cmd,
        stop_event=stop_event,
    )
    if rep is None:
        return None

    collapsed = nsys_stats_to_collapsed(nsys, rep, base)
    if not collapsed:
        return None

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    start = now  # approximate; fine for adhoc labeling
    end = now

    def _gen(collapsed_data: str) -> Optional[str]:
        if generate_html_fn is not None:
            return generate_html_fn(collapsed_data)
        return generate_nsys_flamegraph_html(
            collapsed_data,
            start.isoformat(),
            end.isoformat(),
            generate_html_fn=None,
        )

    html = _gen(collapsed)
    if html:
        logger.info("Generated nsys GPU flamegraph HTML (%d bytes)", len(html))
    else:
        logger.error("Failed to generate nsys flamegraph HTML from collapsed stacks")
    return html
