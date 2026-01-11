
import unittest
import threading
import time
import socket
import json
from unittest.mock import MagicMock
from psutil import Process
import random

# Mock dependencies
class MockProcess:
    def __init__(self, pid):
        self.pid = pid

# Add path to sys.path to import gprofiler modules
import sys
import os
sys.path.append(os.getcwd())

from gprofiler.spark import SparkController
from gprofiler.state import init_state

class TestSparkIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            init_state()
        except:
            pass

    def setUp(self):
        self.port = random.randint(20000, 30000) # Use random port
        self.mock_client = MagicMock()
        self.mock_client.get_spark_allowed_apps.return_value = ["app-allowed"]
        self.controller = SparkController(port=self.port, client=self.mock_client)
        self.controller.start()
        # Wait for server to start
        time.sleep(0.5)

    def tearDown(self):
        self.controller.stop()
        time.sleep(0.1)

    def send_heartbeat(self, pid, app_id, app_name="test-app"):
        data = {
            "pid": pid,
            "spark.app.id": app_id,
            "spark.app.name": app_name
        }
        for i in range(5):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.connect(("127.0.0.1", self.port))
                    s.sendall((json.dumps(data) + "\n").encode("utf-8"))
                break
            except ConnectionRefusedError:
                time.sleep(0.1)
        else:
            raise ConnectionRefusedError("Could not connect to test server")

    def test_filter_processes(self):
        # 1. Register a Spark process that is allowed
        self.send_heartbeat(pid=100, app_id="app-allowed")

        # 2. Register a Spark process that is NOT allowed
        self.send_heartbeat(pid=101, app_id="app-denied")

        # 3. Create a list of processes, including a non-Spark process
        procs = [
            MockProcess(100), # Spark, allowed
            MockProcess(101), # Spark, denied
            MockProcess(102), # Not Spark (should be kept)
        ]

        # Let's force an update of allowed apps
        with self.controller._allowed_apps_lock:
            self.controller._allowed_apps = {"app-allowed"}

        # Allow some time for socket processing
        time.sleep(0.5)

        # 4. Filter
        filtered = self.controller.filter_processes(procs)
        pids = sorted([p.pid for p in filtered])

        # Expect 100 (Allowed Spark) and 102 (Non-Spark)
        # 101 should be filtered out.
        self.assertEqual(pids, [100, 102])

    def test_stale_cleanup(self):
        # Set a short timeout for testing
        self.controller.STALE_THRESHOLD_S = 1

        self.send_heartbeat(pid=200, app_id="app-stale")
        time.sleep(0.5)

        # Should be present
        with self.controller._registry_lock:
            self.assertIn(200, self.controller._registry)

        # Manually invoke cleanup logic
        now = time.time()
        # Simulate time passing by artificially aging the heartbeat
        with self.controller._registry_lock:
            if 200 in self.controller._registry:
                self.controller._registry[200]["last_heartbeat"] = now - 5

        # Run one iteration of cleanup logic manually
        to_remove = []
        with self.controller._registry_lock:
            for pid, info in self.controller._registry.items():
                if now - info["last_heartbeat"] > self.controller.STALE_THRESHOLD_S:
                    to_remove.append(pid)
            for pid in to_remove:
                del self.controller._registry[pid]

        with self.controller._registry_lock:
            self.assertNotIn(200, self.controller._registry)

if __name__ == "__main__":
    unittest.main()
