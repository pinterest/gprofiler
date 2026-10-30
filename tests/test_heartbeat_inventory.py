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
"""Change-detected heartbeat inventory: digest contract shared with the server, and the
client rule for when the full inventory is included in a heartbeat."""

from typing import Any, Dict, Optional

import pytest

from gprofiler.dynamic_profiling_management.heartbeat import HeartbeatClient
from gprofiler.metadata import heartbeat_inventory as hi

# Pinned on both sides. The server's spec (gprofiler-performance-studio,
# src/tests/spec/backend/test_heartbeat_inventory_spec.py) asserts the same two values;
# if one side changes, both tests fail and DIGEST_VERSION must be bumped together.
EMPTY_DIGEST = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
SINGLE_CONTAINER_DIGEST = "aae22d2ec4de790a53542e47a558191f3a08229aaa922051e97b8eb8c29e1b57"


def _container(**overrides: Any) -> Dict[str, Any]:
    container = {
        "container_id": "c1",
        "container_name": "app",
        "runtime": "containerd",
        "namespace": "prod",
        "pod_name": "app-7d9f-abc12",
        "workload_name": "app",
        "workload_kind": "Deployment",
        "processes": [{"pid": 100, "process_name": "java"}, {"pid": 101, "process_name": "sh"}],
    }
    container.update(overrides)
    return container


def test_digest_vectors_match_server() -> None:
    assert hi.DIGEST_VERSION == "1"
    assert hi.inventory_digest([]) == EMPTY_DIGEST
    assert hi.inventory_digest([_container()]) == SINGLE_CONTAINER_DIGEST


def test_not_reported_is_not_empty() -> None:
    assert hi.inventory_digest(None) is None
    assert hi.inventory_digest([]) is not None


def test_digest_ignores_order_and_server_filtered_entries() -> None:
    reordered = _container(processes=[{"pid": 101, "process_name": "sh"}, {"pid": 100, "process_name": "java"}])
    assert hi.inventory_digest([reordered]) == SINGLE_CONTAINER_DIGEST

    with_noise = [
        _container(),
        {"container_id": None, "container_name": "bare", "processes": [{"pid": 1, "process_name": "init"}]},
        _container(),  # duplicate container id: first one wins
    ]
    assert hi.inventory_digest(with_noise) == SINGLE_CONTAINER_DIGEST

    assert (
        hi.inventory_digest(
            [_container(processes=[{"pid": "100", "process_name": "java"}, {"pid": 101, "process_name": "sh"}])]
        )
        == SINGLE_CONTAINER_DIGEST
    )


def test_digest_changes_when_inventory_changes() -> None:
    assert (
        hi.inventory_digest([_container(processes=[{"pid": 100, "process_name": "java"}])]) != SINGLE_CONTAINER_DIGEST
    )
    assert hi.inventory_digest([_container(pod_name="app-7d9f-zzzzz")]) != SINGLE_CONTAINER_DIGEST


# --- client send/acknowledge rule -------------------------------------------------------------


def _client() -> HeartbeatClient:
    # Bypass __init__ (PMU manager, container runtime, network probing); set only what
    # the payload builder reads.
    client = HeartbeatClient.__new__(HeartbeatClient)
    client.ip_address = "10.0.0.1"
    client.hostname = "host-1"
    client.service_name = "svc"
    client.last_command_id = None
    client.received_command_ids = set()
    client.executed_command_ids = set()
    client._acknowledged_inventory_digest = None
    return client


def _beat(
    client: HeartbeatClient, containers: Optional[list], server_reply: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    metadata = {"agent_version": "x", "run_mode": "k8s", "namespace": None, "pod_name": None, "containers": containers}
    payload = client.build_heartbeat_payload(metadata, perf_supported_events=[])
    client.note_heartbeat_response(payload, server_reply)
    return payload


def test_first_beat_sends_full_inventory_with_digest() -> None:
    payload = _beat(_client(), [_container()], {"success": True, "need_inventory": True})
    assert payload["inventory_digest"] == SINGLE_CONTAINER_DIGEST
    assert payload["containers"] == [_container()]


def test_inventory_omitted_only_after_server_confirms_it_holds_it() -> None:
    client = _client()
    inventory = [_container()]

    # Server has not committed it yet (async write / sync deferred): keep sending.
    assert "containers" in _beat(client, inventory, {"success": True, "need_inventory": True})
    assert "containers" in _beat(client, inventory, {"success": True, "need_inventory": True})
    # Server confirms -> digest-only from now on.
    assert "containers" in _beat(client, inventory, {"success": True, "need_inventory": False})
    payload = _beat(client, inventory, {"success": True, "need_inventory": False})
    assert "containers" not in payload and payload["inventory_digest"] == SINGLE_CONTAINER_DIGEST


def test_change_is_sent_in_full_until_confirmed() -> None:
    client = _client()
    _beat(client, [_container()], {"success": True, "need_inventory": False})
    assert "containers" not in _beat(client, [_container()], {"success": True, "need_inventory": False})

    changed = [_container(processes=[{"pid": 100, "process_name": "java"}])]
    payload = _beat(client, changed, {"success": True, "need_inventory": True})
    assert payload["containers"] == changed
    payload = _beat(client, changed, {"success": True, "need_inventory": False})
    assert payload["containers"] == changed  # still full: confirmation arrives with this reply
    assert "containers" not in _beat(client, changed, {"success": True, "need_inventory": False})


def test_server_asking_again_or_failing_resets_to_full() -> None:
    client = _client()
    inventory = [_container()]
    _beat(client, inventory, {"success": True, "need_inventory": False})
    assert "containers" not in _beat(
        client, inventory, {"success": True, "need_inventory": True}
    )  # e.g. server restored from backup
    assert "containers" in _beat(client, inventory, {"success": True, "need_inventory": False})
    assert "containers" not in _beat(client, inventory, None)  # request failed
    assert "containers" in _beat(client, inventory, {"success": True, "need_inventory": False})


@pytest.mark.parametrize("reply", [{"success": True}, {"success": True, "need_inventory": None}])
def test_older_server_without_need_inventory_always_gets_full_inventory(reply: Dict[str, Any]) -> None:
    client = _client()
    for _ in range(3):
        assert "containers" in _beat(client, [_container()], reply)


def test_empty_inventory_is_reported_and_can_be_acknowledged() -> None:
    client = _client()
    payload = _beat(client, [], {"success": True, "need_inventory": False})
    assert payload["containers"] == [] and payload["inventory_digest"] == EMPTY_DIGEST
    assert "containers" not in _beat(client, [], {"success": True, "need_inventory": False})
