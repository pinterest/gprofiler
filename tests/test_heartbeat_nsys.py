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

from pathlib import Path

from gprofiler.dynamic_profiling_management.heartbeat import HeartbeatClient


class _Response:
    status_code = 200

    @staticmethod
    def json():
        return {"success": True}


class _Session:
    def __init__(self):
        self.payload = None

    def post(self, url, json, timeout):
        self.payload = json
        return _Response()


def _client() -> HeartbeatClient:
    client = HeartbeatClient.__new__(HeartbeatClient)
    client.api_server = "http://server"
    client.ip_address = "10.0.0.1"
    client.hostname = "host"
    client.service_name = "svc"
    client.last_command_id = None
    client.received_command_ids = set()
    client.executed_command_ids = set()
    client.pmu_manager = type("Pmu", (), {"get_supported_events": lambda self: []})()
    client.heartbeat_metadata_collector = type("Collector", (), {"collect": lambda self: {"agent_version": "x"}})()
    client.session = _Session()
    return client


def test_heartbeat_reports_nsys_support(monkeypatch):
    monkeypatch.setattr(
        "gprofiler.dynamic_profiling_management.heartbeat.MetricsPublisher.get_instance",
        lambda: type("Publisher", (), {"send_sli_metric": lambda *args, **kwargs: None})(),
    )
    client = _client()

    monkeypatch.setattr("gprofiler.dynamic_profiling_management.heartbeat.find_nsys", lambda: None)
    client.send_heartbeat()
    assert client.session.payload["nsys_available"] is False
    assert client.session.payload["nsys_path"] is None

    monkeypatch.setattr(
        "gprofiler.dynamic_profiling_management.heartbeat.find_nsys",
        lambda: Path("/opt/nvidia/nsight-systems/2025.1.3/target-linux-x64/nsys"),
    )
    client.send_heartbeat()
    assert client.session.payload["nsys_available"] is True
    assert client.session.payload["nsys_path"].endswith("/nsys")
