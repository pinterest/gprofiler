# NVIDIA Nsight Systems (nsys) GPU profiling with gProfiler

## Summary

gProfiler can capture **CUDA kernel** activity via host-installed **`nsys`** and
upload the resulting flamegraph HTML through the existing **adhoc** path
(Performance Studio → Adhoc Profiling view).

This is **not** Intel [iaprof](https://github.com/intel/iaprof) (Xe / Battlemage
EU-stall GPU flame graphs). On NVIDIA hosts, nsys is the correct tool; iaprof
can be a later backend behind the same `enable_nsys`-style control-plane flag.

## Packaging: host-detect, do not bundle

Mirror PerfSpect:

| | Behavior |
|---|---|
| Default | Search `NSYS_PATH`, `PATH`, `/usr/local/bin/nsys`, `/opt/nvidia/nsight-systems/*/bin/nsys` |
| Override | `--nsys-path` / heartbeat `combined_config.nsys_path` |
| Bundle | **No** — Nsight Systems is large and NVIDIA-licensed |

## Enabling

### CLI

```bash
sudo ./gprofiler \
  --upload-results --token=... --service-name=k8s-sandbox \
  --enable-heartbeat-server --api-server=https://localhost:30443 \
  --enable-nsys \
  --nsys-workload='/path/to/cuda_burn 30' \
  --duration=30 --flamegraph
```

### Heartbeat / Dynamic Profiling console

Studio checkbox **GPU (nsys)** sets `additional_args.enable_nsys=true`.
The backend merges that into top-level `combined_config`. Optional:

- `nsys_path`: absolute path to `nsys`
- `nsys_workload`: command for nsys to wrap (strongly recommended)

When `enable_nsys` succeeds, the uploaded `flamegraph_html` is the **GPU**
flamegraph (preferred over CPU). `run_arguments.perf_events` also gains
`nsys-cuda` so the Adhoc UI can show a **GPU / nsys** chip.

## CPU/GPU timeline mode (`--nsys-timeline` / `nsys_timeline`)

The same capture (`-t cuda,nvtx -s none`) already records both sides with
timestamps: `cuda_gpu_trace` (per-kernel Start/Duration/Device/Stream) and
`cuda_api_trace` (per-call Start/Duration/Pid/Tid — `-s none` only disables CPU
*stack sampling*, CUDA API tracing stays on). Both share one nsys clock and a
`CorrID` linking each CPU-side launch to the GPU kernel it produced.

With `--nsys-timeline` (CLI) or `combined_config.nsys_timeline: true`
(heartbeat, alongside `enable_nsys`), the agent exports those two trace reports
instead of the kern/api summaries and uploads a **self-contained timeline HTML**
through the same adhoc path: swim lanes per CPU thread issuing CUDA calls and
per GPU device/stream, wheel-zoom / drag-pan, and click-to-highlight CorrID so a
`cudaLaunchKernel` and its kernel light up together. If the trace export yields
no events, the agent falls back to the GPU flamegraph. `perf_events` also gains
`nsys-timeline` (next to `nsys-cuda`) so the UI can distinguish the view.

Captures with more than 20,000 events keep the longest ones (the HTML notes
"showing N of M") to bound upload size.

The view opens auto-zoomed to a window where the median event is a few pixels
wide (a "Full span" button resets), and at low zoom sub-pixel events shade the
lane by occupancy instead of tiling solid 1px bars — so a fully-packed lane
looks different from a half-idle one even when zoomed out.

## Kind / sandbox topology

Kind runs the Studio control plane only. The agent that invokes nsys must run on
the **GPU host** (see `gprofiler-performance-studio/deploy/k8s-sandbox/gpu/`).

Run that agent in its own container with `--gpus all` rather than as a bare host
process. `grab_gprofiler_mutex()` binds an abstract socket in the init network
namespace, so a bare host agent conflicts with any other privileged gProfiler on
the machine; a separate network namespace avoids the conflict while still giving
nsys direct GPU access. Mount the Nsight Systems install read-only — nsys is
detected, never bundled.

## Architecture: end-to-end pipeline (PyTorch example)

The nsys workload is just a command string, so profiling a real ML framework is
the same pipeline as `cuda_burn` — the only requirement is that the agent's
container can execute the command. For PyTorch that means an agent image with
`python3` + `torch` layered on (see `deploy/k8s-sandbox/gpu/agent-torch.Dockerfile`
in gprofiler-performance-studio).

```
┌────────────────────────── GPU host (e.g. g5.4xlarge / A10G) ─────────────────────────┐
│                                                                                      │
│  ┌── Kind cluster (Studio control plane — no GPU) ─────────────────────────────────┐ │
│  │  webapp ─ indexer ─ Postgres ─ ClickHouse ─ LocalStack("S3")                    │ │
│  │     ▲         │                                   ▲                             │ │
│  │     │         └── writes flamegraph HTML ─────────┘                             │ │
│  └─────┼────────────────────────────────────────────────────────────────────────--┘ │
│        │ kubectl port-forward (bound on all addresses)                              │
│  ┌─────┴── GPU agent container (--gpus all, own network namespace) ──────────────┐  │
│  │  gProfiler agent (PyInstaller exe)                                            │  │
│  │   ① heartbeat receives adhoc profile_request                                  │  │
│  │      { enable_nsys, nsys_workload: "python3 /gpu/torch_workload.py 30" }      │  │
│  │   ② spawns (with _workload_env() cleaned environment):                        │  │
│  │        nsys profile -t cuda,nvtx -s none <workload>                           │  │
│  │        └─ torch dispatches cuBLAS/cutlass + ATen kernels on the GPU           │  │
│  │   ③ nsys stats cuda_gpu_kern_sum → CSV → collapsed stacks                     │  │
│  │   ④ collapsed → flamegraph HTML; perf_events += "nsys-cuda"                   │  │
│  │   ⑤ POST /api/profiles with flamegraph_html in the profile header JSON        │  │
│  │  mounts: workload dir (ro) · Nsight Systems install (ro)                      │  │
│  └───────────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────────────┘
      Browser → Studio Adhoc view: lists HTML files from S3, shows the GPU / nsys
      chip (from nsys-cuda), renders the agent-generated HTML in an iframe.
```

Notes:

- The flamegraph is **generated by the agent** (step ④) and stored as-is; the UI
  replays it in an iframe rather than computing it.
- The agent container reaches Studio via the host gateway (`host.docker.internal`)
  because it deliberately does not share the host network namespace (mutex, see
  topology above).
- The indexer marks the upload adhoc and records `perf_events` metadata
  (`callstacks.go`), which is what drives the Adhoc UI chip.
- Scope: this is kernel-level triage. Framework-side attribution (which module /
  line launched a kernel) is `torch.profiler` territory; per-kernel hardware
  counters are Nsight Compute. A future improvement could surface NVTX ranges
  (already traced via `-t cuda,nvtx`) emitted by `torch.autograd.profiler.emit_nvtx()`
  to add op-level frames above the kernel leaves.

## Collapsed stack shape

```
gpu;nsys;cuda_kernel;burn(float *, int, int) 28918
```

Fallback if kern sum is empty:

```
gpu;nsys;cuda_api;cudaLaunchKernel 213
```

## Reading the flamegraph

- The `gpu;nsys;cuda_kernel` prefix is a constant category, **not a call stack**;
  `cuda_gpu_kern_sum` is a flat per-kernel summary, so each kernel is a leaf and
  the only meaningful axis is width.
- Weights are **GPU time in microseconds** from CUPTI activity tracing (total ns
  per kernel across all launches, divided by 1000) — not samples, despite the
  renderer's "samples" label, and not PMU counters.
- The `nsys-cuda` entry in `perf_events` is a capture-type tag for the Adhoc UI
  chip, not a hardware event.
- For a real ML workload the frames are library kernels the framework dispatched
  (e.g. `cutlass_80_tensorop_*gemm_*` for a PyTorch fp16 matmul on Tensor Cores;
  a `_relu` infix means the activation was fused into the GEMM epilogue).

## Workload environment (PyInstaller caveat)

gProfiler runs as a PyInstaller bundle that prepends its unpack dir
(`/tmp/_MEIxxxx`) to `LD_LIBRARY_PATH`. The nsys workload is spawned with a
cleaned environment (`_workload_env()`): PyInstaller's saved `*_ORIG` values are
restored, and any `_MEI` path is stripped otherwise. Without this, a
**dynamically-linked** workload (python/torch, most real binaries) loads the
bundle's older `libstdc++` and fails to start (`CXXABI_... not found`), yielding
an empty capture; statically-linked workloads are unaffected. If `enable_nsys`
produces a bare `root` flamegraph, check the agent log for
`ImportError`/`CXXABI` from the workload.
