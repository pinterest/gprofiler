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

## Kind / sandbox topology

Kind runs the Studio control plane only. The agent that invokes nsys must run on
the **GPU host** (see `gprofiler-performance-studio/deploy/k8s-sandbox/gpu/`).

Run that agent in its own container with `--gpus all` rather than as a bare host
process. `grab_gprofiler_mutex()` binds an abstract socket in the init network
namespace, so a bare host agent conflicts with any other privileged gProfiler on
the machine; a separate network namespace avoids the conflict while still giving
nsys direct GPU access. Mount the Nsight Systems install read-only — nsys is
detected, never bundled.

## Collapsed stack shape

```
gpu;nsys;cuda_kernel;burn(float *, int, int) 28918
```

Fallback if kern sum is empty:

```
gpu;nsys;cuda_api;cudaLaunchKernel 213
```
