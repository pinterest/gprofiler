
import unittest
import threading
import time
import json
import requests
from unittest.mock import MagicMock
from psutil import Process
import random
import sys
import os

sys.path.append(os.getcwd())

from gprofiler.spark import SparkController
from gprofiler.state import init_state

class MockProcess:
    def __init__(self, pid):
        self.pid = pid

class TestSparkIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            init_state()
        except:
            pass

    def setUp(self):
        self.port = random.randint(20000, 30000)
        self.mock_client = MagicMock()
        self.mock_client.get_spark_allowed_apps.return_value = ["app-allowed"]
        self.controller = SparkController(port=self.port, client=self.mock_client)
        self.controller.start()
        time.sleep(0.5)

    def tearDown(self):
        self.controller.stop()
        time.sleep(0.1)

    def send_heartbeat_receive_response(self, pid, app_id, app_name="test-app"):
        data = {
            "pid": pid,
            "spark.app.id": app_id,
            "spark.app.name": app_name
        }
        url = f"http://127.0.0.1:{self.port}/spark"
        for i in range(5):
            try:
                resp = requests.post(url, json=data, timeout=2)
                return resp.json()
            except requests.RequestException:
                time.sleep(0.1)
        raise ConnectionRefusedError("Could not connect to test server")

    def send_thread_info(self, pid, app_id, threads):
        data = {
            "type": "thread_info",
            "pid": pid,
            "spark.app.id": app_id,
            "threads": threads
        }
        url = f"http://127.0.0.1:{self.port}/spark"
        requests.post(url, json=data, timeout=2)

    def test_filter_processes_and_handshake(self):
        # Allow apps
        with self.controller._allowed_apps_lock:
            self.controller._allowed_apps = {"app-allowed"}

        # 1. Heartbeat from allowed app
        resp = self.send_heartbeat_receive_response(pid=100, app_id="app-allowed")
        self.assertTrue(resp.get("profile"))

        # 2. Heartbeat from denied app
        resp = self.send_heartbeat_receive_response(pid=101, app_id="app-denied")
        self.assertFalse(resp.get("profile"))

        # 3. Filter check
        procs = [MockProcess(100), MockProcess(101), MockProcess(102)]
        filtered = self.controller.filter_processes(procs)
        pids = sorted([p.pid for p in filtered])
        self.assertEqual(pids, [100, 102])

    def test_thread_info_update(self):
        # Send heartbeat first to establish entry
        self.send_heartbeat_receive_response(pid=200, app_id="app-allowed")

        # Send thread info
        threads = [{"tid": 1, "name": "main"}, {"tid": 2, "name": "spark-executor"}]
        self.send_thread_info(pid=200, app_id="app-allowed", threads=threads)

        time.sleep(0.2)

        with self.controller._registry_lock:
            self.assertIn(200, self.controller._registry)
            self.assertIn("threads", self.controller._registry[200])
            thread_map = self.controller._registry[200]["threads"]
            self.assertEqual(thread_map[1], "main")
            self.assertEqual(thread_map[2], "spark-executor")

if __name__ == "__main__":
    unittest.main()
