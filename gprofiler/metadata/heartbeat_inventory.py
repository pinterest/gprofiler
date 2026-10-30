#
# Copyright (C) 2022 Intel Corporation
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
"""Fingerprint of the heartbeat workload inventory, shared with the Performance Studio server.

The agent heartbeats every 30 s. Its container / process inventory almost never changes
between two beats, so instead of shipping the whole list every time the agent sends a
sha256 digest of it and includes the list only when the digest differs from what the
server has confirmed it holds (or when the server asks, ``need_inventory: true``).

This must stay byte-for-byte identical to the server's implementation
(``gprofiler_dev/heartbeat_inventory.py`` in gprofiler-performance-studio). The canonical
form is deliberately simple and language-neutral:

    [ [container_id, container_name, runtime, namespace, pod_name, workload_name,
       workload_kind, [[pid, process_name], ...]], ... ]

sorted by container_id, processes sorted by pid, JSON-encoded with ``separators=(",", ":")``
and ``ensure_ascii=False``, UTF-8, sha256. Entries the server would not store are excluded
*before* hashing (containers without a ``container_id``, duplicate container ids,
non-integer pids, duplicate pids) and empty strings become ``null``, so the digest
describes exactly what the server ends up holding.

A mismatch between the two implementations only costs efficiency, never correctness: the
server always re-derives the digest from the inventory it actually stores and keeps asking
for the full inventory until the agent's digest matches, so the agent simply falls back to
sending everything every beat (today's behaviour).
"""

import hashlib
import json
from typing import Any, Iterable, List, Mapping, Optional

DIGEST_VERSION = "1"
"""Bump in lockstep with the server when the canonical form changes."""


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def canonical_inventory(containers: Iterable[Mapping[str, Any]]) -> List[List[Any]]:
    """Return the canonical (sorted, filtered, positional) form of an inventory payload."""
    by_cid: dict = {}
    for container in containers or []:
        if not isinstance(container, Mapping):
            continue
        cid = _opt_str(container.get("container_id"))
        if cid is None or cid in by_cid:
            continue
        procs: dict = {}
        for process in container.get("processes") or []:
            if not isinstance(process, Mapping):
                continue
            raw_pid = process.get("pid")
            if raw_pid is None or not str(raw_pid).isdigit():
                continue
            pid = int(raw_pid)
            if pid in procs:
                continue
            procs[pid] = _opt_str(process.get("process_name"))
        by_cid[cid] = [
            cid,
            _opt_str(container.get("container_name")),
            _opt_str(container.get("runtime")),
            _opt_str(container.get("namespace")),
            _opt_str(container.get("pod_name")),
            _opt_str(container.get("workload_name")),
            _opt_str(container.get("workload_kind")),
            [[pid, procs[pid]] for pid in sorted(procs)],
        ]
    return [by_cid[cid] for cid in sorted(by_cid)]


def inventory_digest(containers: Optional[Iterable[Mapping[str, Any]]]) -> Optional[str]:
    """sha256 hex digest of the canonical inventory; ``None`` when there is no inventory to report.

    ``[]`` (this host has no containers) is a real inventory with a real digest; ``None``
    means the field is not being reported at all.
    """
    if containers is None:
        return None
    payload = json.dumps(canonical_inventory(containers), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
