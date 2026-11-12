# gProfiler GPU profiling — phases and milestones

Program-level plan for turning the NVIDIA `nsys` prototype
([gprofiler#77](https://github.com/pinterest/gprofiler/pull/77),
[gprofiler-performance-studio#86](https://github.com/pinterest/gprofiler-performance-studio/pull/86))
into a supported GPU profiling capability.

- **Phases** describe *what kind of work* and are the unit of scope.
- **Milestones** are *dated commitments* and are the unit of reporting.

Implementation detail for what already exists lives in
[`NSYS_GPU_PROFILING.md`](./NSYS_GPU_PROFILING.md); the sandbox topology and the
prototype build stages live in the Studio repo under
`deploy/k8s-sandbox/gpu/DESIGN.md`.

## Why GPU profiling, in one line

GPU utilization is bounded by inference and training latency, and today nothing
attributes *where that latency goes* on the GPU. Telemetry says a GPU is 25%
busy; it cannot say whether the remaining 75% is idle waiting on the host input
pipeline, lost to launch overhead, or spent in memory-bound kernels. Profiling
is what converts a utilization number into an actionable change.

---

## Phases

### Phase 1 — Discoverability and safe capture

Make GPU profiling *findable* and *safe to run* on a real fleet. A user with a
GPU host should be able to discover that GPU profiling exists, learn whether
their host supports it, and get a bounded capture with a downloadable artifact —
without a human walking them through it.

Explicitly includes preflight that **fails with remediation** rather than
failing during collection, and hard caps so a mistaken request cannot damage a
production host.

### Phase 2 — Safe UI-triggered MVP with CPU↔GPU correlation

The operator-facing product: trigger a bounded Nsight trace from the Studio UI,
scoped to a workload, with duration / overhead budget / cancel semantics reusing
the existing dynamic-profiling command model. Adds the first *correlated* view —
a FlameScope-style heatmap-to-flamegraph workflow that lines CPU bursts up
against GPU idle gaps, which is the specific signal that distinguishes "GPU is
slow" from "GPU is starved".

Adhoc by default, not continuous-by-default. Continuous GPU tracing is out of
scope for the MVP.

### Phase 3 — Telemetry, ingestion, and first-class visualization

Move GPU data from "an artifact you download" to "a first-class entity in
gProfiler". Continuous low-overhead NVML/DCGM telemetry, artifact ingestion into
a versioned schema, and trace/timeline views that live inside the product rather
than behind an iframe.

Critically, GPU records **join** existing gProfiler entities (service, host,
process, container, commit) rather than forking a parallel metadata model.

### Phase 4 — Deep kernel analysis and fleet coverage (later)

Nsight Compute kernel-level analysis, MIG and MPS shared-device support,
multi-GPU and NCCL collective correlation, cross-commit regression comparison,
and evidence-linked optimization recommendations.

---

## Milestones

| Milestone | Date | Phase | Committed deliverables |
|---|---|---|---|
| **M1** | 2026-09-30 | Phase 1 | Prototype landed in Pinterest repos (#77 / #86). GPU + `nsys`/NVML preflight with actionable failure messages. Raw `.nsys-rep` uploaded to S3 with a download path. Duration and artifact-size caps enforced agent-side. Working demo on a PinCompute GPU host plus an operator runbook. |
| **M2** | 2026-10-31 | Phase 2 | UI-triggered bounded Nsight traces (adhoc). Command model extended with duration, overhead budget, and cancel/kill; target GPU where known. FlameScope prototype correlating CPU bursts against GPU idle gaps. Host record carries GPU UUID and model. |
| **M3** | 2026-12-31 | Phase 3 | NVML/DCGM telemetry at a configurable interval on PinCompute GPU hosts. Host GPU inventory (UUID, model, memory, driver/CUDA). Artifacts ingested into existing gProfiler entities. Trace/timeline views beyond the adhoc iframe. Compatibility validated on one GPU SKU + driver/CUDA pair, with explicit fallback when `nsys` is unavailable. |
| **M4** | H1 2027 | Phase 4 | Nsight Compute, MIG/MPS, multi-GPU/NCCL, regression comparison, automated recommendations. **Not an H2 2026 commitment.** |

### Milestone guardrails

- **M1 is the discoverability gate.** If a GPU host cannot self-report whether
  profiling will work, nothing downstream is usable at fleet scale.
- **GPU UUID/model on the host record is an M2 item, not an M1 gate.**
- **M3 compatibility is one SKU + one driver/CUDA pair**, deliberately. A full
  matrix is Phase 4 work; committing to it in 2026 would be fiction.
- Every milestone ships with a fallback path. Degrading from deep profiling to
  supported telemetry with an explicit status message is a deliverable, not a
  bug.

---

## Backlog mapping

Numbers refer to the GPU enhancement backlog. Estimates are engineering-weeks
for one engineer, and assume the prototype as the starting point.

| # | Backlog item | Phase | Est. | Owner |
|---|---|---|---|---|
| 1 | GPU discovery and capability detection | 1 → 2 | 3 | A |
| 2 | Agent packaging and runtime dependencies | 1 | 2 | A |
| 3 | Permissions and prerequisite validation | 1 | 2 | A |
| 8 | Nsight Systems trace collection | 1 → 2 | 3 | A |
| 11 | GPU profile artifact management | 1 → 3 | 3 | B |
| 20 | Overhead and production safety controls | 1 → 2 | 3 | A |
| 23 | Error handling and diagnostics | 1 → 3 | 3 | A |
| 28 | Documentation and operational runbooks | 1 → 3 | 2 | B |
| 7 | Dynamic GPU profiling command model | 2 | 3 | B |
| 13 | CPU and GPU timeline correlation | 2 → 3 | 5 | B |
| 27 | Validation workloads and test automation | 2 → 3 | 4 | A + B |
| 4 | GPU telemetry collection (NVML/DCGM) | 3 | 4 | A |
| 12 | GPU profile ingestion and normalization | 3 | 5 | B |
| 14 | GPU kernel and call-path visualization | 3 | 4 | B |
| 22 | Compatibility matrix and graceful fallback | 3 | 3 | A |
| 24 | Backend APIs and query model | 3 | 4 | B |
| 5 | MIG support | 4 | 4 | A |
| 6 | MPS and shared-GPU support | 4 | 3 | A |
| 9 | Nsight Compute kernel profiling | 4 | 5 | A |
| 10 | CUPTI native collection integration | 4 | 6 | A |
| 15 | GPU bottleneck metrics view | 4 | 4 | B |
| 16 | Source code correlation | 4 | 4 | B |
| 17 | GPU profile comparison and regression analysis | 4 | 5 | B |
| 18 | Automated GPU recommendations | 4 | 5 | B |
| 19 | Distributed and multi-GPU workload support | 4 | 6 | A + B |

Rolled up: Phase 1 ≈ 10 eng-weeks, Phase 2 ≈ 11, Phase 3 ≈ 18, Phase 4 ≈ 42.

---

## Staffing: two contractors

The work splits cleanly along the agent / control-plane boundary, which is
already how the two repos are organized. That boundary keeps the two streams
from blocking each other after the command-model contract is agreed in Phase 1.

### Contractor A — Agent and capture (`gprofiler`)

Owns everything that runs on the GPU host: detection, packaging, permissions,
capture backends, and production safety.

- Phase 1: capability detection, preflight, packaging, duration/size caps
- Phase 2: bounded capture, cancel/kill, overhead budget enforcement
- Phase 3: NVML/DCGM telemetry, compatibility matrix, fallback behavior
- Phase 4: Nsight Compute, CUPTI, MIG/MPS

Profile: systems engineer comfortable with CUDA tooling, Linux capabilities and
container device plumbing, and Kubernetes security contexts.

### Contractor B — Control plane, ingestion, and UI (`gprofiler-performance-studio`)

Owns everything from the uploaded artifact inward: command model, storage,
schema, query APIs, and visualization.

- Phase 1: artifact upload/download, runbook
- Phase 2: GPU fields in the dynamic-profiling command model, FlameScope
  correlation prototype
- Phase 3: ingestion and normalization, joins to existing entities, backend APIs,
  trace/timeline views
- Phase 4: bottleneck metrics, source correlation, regression comparison,
  recommendations

Profile: backend engineer with data-modeling depth, comfortable in React for the
visualization surface.

### Shared

Validation workloads (item 27) are shared — Contractor A builds the CUDA
workloads (compute-bound, memory-bound, transfer-bound, sync-bound, tensor-core,
failure cases), Contractor B wires them into end-to-end Kubernetes tests with
golden outputs.

### Sequencing note

One contractor can deliver M1 and M2. The second is what makes M3 reachable by
2026-12-31 — Phase 3 is roughly 18 eng-weeks against a ~9-week window from M2,
which does not fit a single engineer. Onboard the second contractor no later
than M2 so the ingestion schema work starts in parallel with the MVP.

---

## Non-goals

- Continuous, always-on GPU tracing. Nsight owns CUPTI while it runs; the
  overhead and the exclusivity make continuous tracing the wrong default.
- Simultaneous `nsys` and `torch.profiler` capture, for the same reason.
- Bundling Nsight Systems into the agent image. Host-detect only, mirroring
  PerfSpect.
- Intel GPU support (`iaprof`). Tracked as a possible later backend behind the
  same control-plane flag.
