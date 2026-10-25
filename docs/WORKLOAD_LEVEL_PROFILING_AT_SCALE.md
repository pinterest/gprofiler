# Workload-level profiling at scale: the agent side

This is the parent document for the agent half of making workload-level (dynamic)
profiling work at fleet scale — on the order of 150k hosts and 100M processes. The code
change is [pinterest/gprofiler #87](https://github.com/pinterest/gprofiler/pull/87)
(branch `heartbeat-change-detected-inventory`). The server half is a five-PR stack in
Performance Studio, explained in
[`heartbeat_doc/WORKLOAD_PROFILING_AT_SCALE.md`](https://github.com/pinterest/gprofiler-performance-studio/blob/master/heartbeat_doc/WORKLOAD_PROFILING_AT_SCALE.md)
in that repo. The agent contract lives in
[`WORKLOAD_LEVEL_PROFILING_SPEC.md`](WORKLOAD_LEVEL_PROFILING_SPEC.md); this document is
the "why" and the map.

## 1. Why heartbeats were heavy

Every agent sends a heartbeat every 30 seconds so that start/stop commands reach it within
30 seconds. Workload-level profiling added the host's **inventory** to that heartbeat — the
list of containers and, for each container, its processes (pid, name) — so the server can
turn "profile this pod" into "profile these pids on this host".

The first version sent the complete inventory on **every** beat, whether anything had
changed or not. Inventory changes rarely (a deploy, a pod reschedule). So the fleet
re-sent, and the server re-stored, the same data every 30 seconds:

| Fleet | Beats per second | Process rows re-sent per 30 s | Payload per beat |
| --- | --- | --- | --- |
| ~33k hosts, ~3M processes | ~1,100 | ~3M | ~50–500 KB |
| 150k hosts, 100M processes (target) | ~5,000 | ~100M | — |

Server-side work was proportional to inventory size × beat rate. Fifteen server PRs tried
to make the database absorb that faster; none made it smaller. This was already named as
the highest-leverage remaining change in the spec's Future Extensions ("change-detected
inventory"); #87 implements it.

## 2. What the agent does now

### 2.1 Fingerprint the inventory

`gprofiler/metadata/heartbeat_inventory.py` turns the `containers` list into a canonical
form — containers sorted by id, processes sorted by pid, the same filters the server's
sync applies, entries the server would not store excluded — serialises it as compact JSON
and takes a sha256. `DIGEST_VERSION = "1"` is part of the hashed input. The server has the
same function byte-for-byte (`gprofiler_dev/heartbeat_inventory.py` in Performance Studio)
and the two share pinned test vectors, so both sides compute the same digest for the same
inventory.

### 2.2 Send the digest every beat, the containers only on change

In `HeartbeatClient` (`gprofiler/dynamic_profiling_management/heartbeat.py`):

- every heartbeat carries `inventory_digest`
- `containers` is included only when the digest differs from the last **acknowledged**
  digest
- a `containers` key that is absent means "not reported this time"; `containers: []` means
  "I have no containers". The server treats these differently (absent → no inventory work;
  `[]` → prune stored rows), so the agent never substitutes one for the other

An unchanged beat shrinks from tens or hundreds of kilobytes to a few hundred bytes.

### 2.3 The acknowledgement rule

The agent acknowledges a digest **only** on an explicit `need_inventory: false` in the
server's reply. Anything else — the field absent (old server), a non-200 response, an
exception — clears the acknowledgement, and the next beat carries the full `containers`
again.

```
reply has need_inventory: false  →  remember this digest; send digest only while it holds
reply has need_inventory: true   →  server does not hold it yet; send full inventory next beat
field absent / error             →  forget; send full inventory next beat
```

Consequences:

- **Agent restart** costs one full beat (the in-memory acknowledgement is gone), then the
  server compares and replies `false`.
- **Server lost state** (database restored, deferred sync, new column) is answered with
  `need_inventory: true` until the server really holds the inventory. The agent cannot get
  stuck believing the server has something it does not.
- **Old server** that does not know `inventory_digest` ignores the unknown field and never
  sends `need_inventory`, so containers keep flowing on every beat — today's behaviour,
  nothing lost.
- **New server, old agent**: no digest in the request → the server computes its own from
  the full payload and replies `need_inventory: null`. Also today's behaviour.

The agent's own process is deliberately *not* excluded from the inventory: the agent
reports what it sees and the server stores what it gets, so the two sides cannot drift on
a special-case rule. An agent restart therefore changes one pid and costs one small diff on
the server.

### 2.4 Command control is unchanged

The server looks up the current profiling command and runs auto-subscribe on every beat
regardless of what the payload carries, and the reply carries the command exactly as
before. Verified end to end against the new server with this client:

| Scope | What was sent | What arrived on the next digest-only beat |
| --- | --- | --- |
| service | start for a service (no selector) | every host of the service got start, then stop |
| host | start for one named host | only that host got the command |
| pod | start for one pod | the host running the pod got start with that pod's pids in `combined_config.pids` |
| pod, host not yet synced | start for a pod on a host whose first sync the server deferred | no command (correct — the server does not know the pid list yet); a new request after the sync picks it up |

Command execution, idempotency, the priority queue and the ack lists are untouched.

## 3. What the server does with it (short version)

- Compares the digest inside the one-row `UPDATE` it already runs for every heartbeat; an
  unchanged beat touches zero inventory rows and takes ~1 ms of database time.
- Admits full syncs through a per-process budget (`GPROFILER_INVENTORY_SYNC_PER_SEC`), so a
  fleet rollout or a database restore turns into a paced one-time load (~22 minutes for
  150k hosts at the default budget) instead of a herd. A deferred host is told
  `need_inventory: true` and simply asks again 30 seconds later.
- Stores its own computed digest as the source of truth; the agent's digest is only
  compared. Version skew degrades to "send full every beat", never to wrong inventory.
- Exposes `inventory_pending_hosts` (fresh hosts with no committed digest) so rollout
  progress is visible on the Profiling Status page.

## 4. Rollout order

1. Server stack deployed first (studio #116–#118 at minimum). The agent change is safe
   against older servers but saves nothing until the server replies `need_inventory`.
2. Then roll #87 to agents at any pace. Watch heartbeat payload size (~300 bytes on an
   unchanged beat) and the share of beats that carry `containers` (should track the real
   inventory change rate, a few percent).

No agent configuration changes. No new flags.

## 5. Tests

`tests/test_heartbeat_inventory.py` covers: the pinned digest vectors shared with the
server; `None` vs `[]`; the acknowledgement rule (acknowledge only on explicit `false`;
absent field, `true`, non-200 and exception all reset); digest-only beats after an
acknowledgement; full payload again after a change.
