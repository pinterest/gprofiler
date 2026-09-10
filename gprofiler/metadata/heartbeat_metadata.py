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

import os
import re
import time
from typing import Any, Dict, List, Optional

from granulate_utils.containers.client import ContainersClient
from granulate_utils.exceptions import NoContainerRuntimesError
from granulate_utils.linux.containers import get_process_container_id
from psutil import NoSuchProcess, Process, process_iter

from gprofiler import __version__
from gprofiler.log import get_logger_adapter
from gprofiler.metadata.system_metadata import get_run_mode

logger = get_logger_adapter(__name__)

# Container-runtime (CRI) labels that carry an owner/workload name, most
# authoritative first. On vanilla clusters the pod's user labels are exposed
# here; some distributions (e.g. Pinterest CRDs) surface the owning workload
# name under a vendor key instead, so we probe several before giving up.
WORKLOAD_NAME_LABELS = (
    "app.kubernetes.io/name",
    "app.kubernetes.io/instance",
    "app",
    "k8s-app",
    "pinterest.com/crd_name",
)
# Sentinel label values that carry no real workload name.
_PLACEHOLDER_LABEL_VALUES = frozenset({"unknown", "none", ""})

# Kubernetes derives generated pod-name suffixes (the ReplicaSet
# pod-template-hash and the trailing random token) from a vowel-free "safe"
# alphabet to avoid forming words. Matching that exact alphabet keeps us from
# stripping legitimate tokens (e.g. "-redis", "-mysql") off standalone names.
_K8S_RAND = "bcdfghjklmnpqrstvwxz2456789"
# Deployment/ReplicaSet pod: <name>-<pod-template-hash>-<random-suffix>.
REPLICASET_SUFFIX_RE = re.compile(rf"^(?P<name>.+)-[{_K8S_RAND}]{{6,10}}-[{_K8S_RAND}]{{5}}$")
# StatefulSet pod: <name>-<ordinal>.
STATEFULSET_SUFFIX_RE = re.compile(r"^(?P<name>.+)-\d+$")
# DaemonSet / ReplicationController / bare generateName pod: <name>-<random-suffix>.
DAEMONSET_SUFFIX_RE = re.compile(rf"^(?P<name>.+)-[{_K8S_RAND}]{{5}}$")

_POD_NAME_SUFFIX_RES = (REPLICASET_SUFFIX_RE, STATEFULSET_SUFFIX_RE, DAEMONSET_SUFFIX_RE)


def _best_effort_workload_name(pod_name: Optional[str], labels: Dict[str, str]) -> Optional[str]:
    for key in WORKLOAD_NAME_LABELS:
        value = labels.get(key)
        if value and value.lower() not in _PLACEHOLDER_LABEL_VALUES:
            return value

    if pod_name is None:
        return None

    for suffix_re in _POD_NAME_SUFFIX_RES:
        match = suffix_re.match(pod_name)
        if match is not None:
            return str(match.group("name"))

    return pod_name


class HeartbeatMetadataCollector:
    def __init__(self, refresh_interval_seconds: int = 30) -> None:
        self._refresh_interval_seconds = refresh_interval_seconds
        self._last_snapshot_at = 0.0
        self._last_snapshot: Dict[str, Any] = {
            "agent_version": __version__,
            "run_mode": get_run_mode(),
            "namespace": os.environ.get("POD_NAMESPACE"),
            "pod_name": os.environ.get("POD_NAME"),
            "containers": [],
        }
        try:
            self._containers_client: Optional[ContainersClient] = ContainersClient()
        except NoContainerRuntimesError:
            logger.info("No container runtime found for heartbeat workload inventory")
            self._containers_client = None

    def collect(self) -> Dict[str, Any]:
        now = time.monotonic()
        if now - self._last_snapshot_at < self._refresh_interval_seconds:
            return self._last_snapshot

        containers = self._collect_containers()
        self._last_snapshot = {
            "agent_version": __version__,
            "run_mode": get_run_mode(),
            "namespace": os.environ.get("POD_NAMESPACE"),
            "pod_name": os.environ.get("POD_NAME"),
            "containers": containers,
        }
        self._last_snapshot_at = now
        return self._last_snapshot

    def _collect_containers(self) -> List[Dict[str, Any]]:
        if self._containers_client is None:
            return []

        try:
            containers = list(self._containers_client.list_containers())
        except Exception:
            logger.warning("Failed to enumerate containers for heartbeat inventory", exc_info=True)
            return []

        processes_by_container: Dict[str, List[Dict[str, Any]]] = {}
        for process in process_iter(["pid", "name"]):
            try:
                container_id = get_process_container_id(Process(process.pid))
            except NoSuchProcess:
                continue
            except Exception:
                continue

            if container_id is None:
                continue

            processes_by_container.setdefault(container_id, []).append(
                {
                    "pid": process.pid,
                    "process_name": process.info.get("name") or "",
                }
            )

        workload_inventory: List[Dict[str, Any]] = []
        for container in containers:
            labels = getattr(container, "labels", {}) or {}
            namespace = labels.get("io.kubernetes.pod.namespace")
            pod_name = labels.get("io.kubernetes.pod.name")
            container_name = labels.get("io.kubernetes.container.name") or getattr(container, "name", None)

            workload_inventory.append(
                {
                    "container_id": getattr(container, "id", None),
                    "container_name": container_name,
                    "runtime": getattr(container, "runtime", None),
                    "namespace": namespace,
                    "pod_name": pod_name,
                    "workload_name": _best_effort_workload_name(pod_name, labels),
                    "workload_kind": "k8s" if namespace or pod_name else "container",
                    "processes": sorted(
                        processes_by_container.get(getattr(container, "id", ""), []),
                        key=lambda process_info: process_info["pid"],
                    ),
                }
            )

        return workload_inventory
