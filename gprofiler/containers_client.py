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
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, asdict

from granulate_utils.containers.client import ContainersClient
from granulate_utils.containers.container import Container
from granulate_utils.exceptions import NoContainerRuntimesError
from granulate_utils.linux.containers import get_process_container_id
from psutil import NoSuchProcess, Process

from gprofiler.log import get_logger_adapter
from gprofiler.utils.perf import valid_perf_pid

logger = get_logger_adapter(__name__)


@dataclass
class ContainerInfo:
    pid: Optional[int]
    name: Optional[str]

    @staticmethod
    def from_container(container: Container) -> "ContainerInfo":
        return ContainerInfo(
            pid=container.process.pid if container.process else None,
            name=container.labels.get("io.kubernetes.container.name", container.name)
        )


@dataclass
class PodInfo:
    pod_name: Optional[str]
    containers: List[ContainerInfo]

    @staticmethod
    def from_container(container: Container) -> "PodInfo":
        pod_name = container.labels.get("io.kubernetes.pod.name", None)
        containers = [ContainerInfo.from_container(container)]
        return PodInfo(
            pod_name=pod_name,
            containers=containers
        )

    def add_container(self, container: Container) -> None:
        container_info = ContainerInfo.from_container(container)
        self.containers.append(container_info)

    def merge(self, other: "PodInfo") -> None:
        if other.pod_name != self.pod_name:
            raise ValueError("Cannot merge PodInfo with different pod names")
        self.containers.extend(other.containers)


class NamespaceInfo:
    def __init__(self, namespace: Optional[str], pods: List[PodInfo]) -> None:
        self.namespace: Optional[str] = namespace
        self._pods: Dict[str, PodInfo] = {}
        for pod in pods:
            self.add_pod_info(pod)

    @staticmethod
    def from_container(container: Container) -> "NamespaceInfo":
        namespace = container.labels.get("io.kubernetes.pod.namespace", None)
        pod = PodInfo.from_container(container)
        return NamespaceInfo(
            namespace=namespace,
            pods=[pod]
        )

    def add_pod_info(self, pod: PodInfo) -> None:
        if pod.pod_name not in self._pods:
                self._pods[pod.pod_name] = pod
        else:
            self._pods[pod.pod_name].merge(pod)

    def add_container(self, container: Container) -> None:
        pod = PodInfo.from_container(container)
        self.add_pod_info(pod)

    def merge(self, other: "NamespaceInfo") -> None:
        if other.namespace != self.namespace:
            raise ValueError("Cannot merge NamespaceInfo with different namespaces")
        for pod in other._pods.values():
            self.add_pod_info(pod)

    def to_dict(self) -> Dict:
        return {
            "namespace": self.namespace,
            "pods": [asdict(pod) for pod in self._pods.values()]
        }


class K8sInfo:
    def __init__(self, namespaces: List[NamespaceInfo]) -> None:
        self._namespaces: Dict[str, NamespaceInfo] = {}
        for namespace in namespaces:
            self.add_namespace_info(namespace)

    @staticmethod
    def from_container(container: Container) -> "K8sInfo":
        namespace = NamespaceInfo.from_container(container)
        return K8sInfo(
            namespaces=[namespace]
        )

    def add_namespace_info(self, namespace: NamespaceInfo) -> None:
        if namespace.namespace not in self._namespaces:
            self._namespaces[namespace.namespace] = namespace
        else:
            self._namespaces[namespace.namespace].merge(namespace)

    def add_container(self, container: Container) -> None:
        namespace = NamespaceInfo.from_container(container)
        self.add_namespace_info(namespace)

    def merge(self, other: "K8sInfo") -> None:
        for namespace in other._namespaces.values():
            self.add_namespace_info(namespace)

    def to_dict(self) -> Dict:
        return {
            "namespaces": [ns.to_dict() for ns in self._namespaces.values()]
        }


class ContainerNamesClient:
    def __init__(self) -> None:
        try:
            self._containers_client: Optional[ContainersClient] = ContainersClient()
            logger.info(f"Discovered container runtimes: {self._containers_client.get_runtimes()}")
        except NoContainerRuntimesError:
            logger.warning(
                "Could not find a Docker daemon or CRI-compatible daemon, profiling data will not"
                " include the container names. If you do have a containers runtime and it's not supported,"
                " please open a new issue here:"
                " https://github.com/intel/gprofiler/issues/new"
            )
            self._containers_client = None

        self._pid_to_container_name_cache: Dict[int, str] = {}
        self._current_container_names: Set[str] = set()
        self._container_id_to_name_cache: Dict[str, Optional[str]] = {}

    def reset_cache(self) -> None:
        self._pid_to_container_name_cache.clear()
        self._current_container_names.clear()

    @property
    def container_names(self) -> List[str]:
        return list(self._current_container_names)

    def get_container_name(self, pid: int) -> str:
        if self._containers_client is None:
            return ""

        if not valid_perf_pid(pid):
            return ""

        if pid in self._pid_to_container_name_cache:
            return self._pid_to_container_name_cache[pid]

        container_name: Optional[str] = self._safely_get_process_container_name(pid)
        if container_name is None:
            self._pid_to_container_name_cache[pid] = ""
            return ""

        self._pid_to_container_name_cache[pid] = container_name
        return container_name

    def _safely_get_process_container_name(self, pid: int) -> Optional[str]:
        try:
            try:
                container_id = get_process_container_id(Process(pid))
                if container_id is None:
                    return None
            except NoSuchProcess:
                return None
            return self._get_container_name(container_id)
        except Exception:
            logger.warning(f"Could not get a container name for PID {pid}", exc_info=True)
            return None

    def _get_container_name(self, container_id: str) -> Optional[str]:
        if container_id in self._container_id_to_name_cache:
            container_name = self._container_id_to_name_cache[container_id]
            if container_name is not None:
                # Might happen a few times for the same container name, so we use a set to have unique values
                self._current_container_names.add(container_name)
            return container_name

        self._refresh_container_names_cache()
        if container_id not in self._container_id_to_name_cache:
            self._container_id_to_name_cache[container_id] = None
            return None
        container_name = self._container_id_to_name_cache[container_id]
        if container_name is not None:
            self._current_container_names.add(container_name)
        return container_name

    def _refresh_container_names_cache(self) -> None:
        # We re-fetch all of the currently running containers, so in order to keep the cache small we clear it
        self._container_id_to_name_cache.clear()
        for container in self._containers_client.list_containers() if self._containers_client is not None else []:
            self._container_id_to_name_cache[container.id] = container.name

    def get_k8s_info(self) -> K8sInfo:
        k8s_info = K8sInfo(namespaces=[])

        if self._containers_client is None:
            return k8s_info

        for container in self._containers_client.list_containers():
            k8s_info.add_container(container)

        return k8s_info
