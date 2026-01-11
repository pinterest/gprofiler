import json
import logging
import socket
import threading
import time
from typing import Dict, List, Optional, Set

from psutil import Process

from gprofiler.client import ProfilerAPIClient
from gprofiler.log import get_logger_adapter

logger = get_logger_adapter(__name__)


class SparkController:
    # 300 seconds timeout for stale heartbeats
    STALE_THRESHOLD_S = 300
    # Update allowed apps list every 60 seconds
    BACKEND_POLL_INTERVAL_S = 60

    def __init__(self, port: int = 12345, client: Optional[ProfilerAPIClient] = None):
        self._port = port
        self._client = client
        # Map PID -> {"app_id": str, "last_heartbeat": float, "threads": {tid: name}}
        self._registry: Dict[int, Dict] = {}
        self._registry_lock = threading.Lock()

        self._allowed_apps: Set[str] = set()
        self._allowed_apps_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._server_thread: Optional[threading.Thread] = None
        self._poller_thread: Optional[threading.Thread] = None
        self._cleanup_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop_event.clear()

        self._server_thread = threading.Thread(target=self._run_server, name="SparkSocketServer", daemon=True)
        self._server_thread.start()

        if self._client:
            self._poller_thread = threading.Thread(target=self._run_backend_poller, name="SparkBackendPoller", daemon=True)
            self._poller_thread.start()

        self._cleanup_thread = threading.Thread(target=self._run_cleanup, name="SparkRegistryCleanup", daemon=True)
        self._cleanup_thread.start()

        logger.info(f"SparkController started on port {self._port}")

    def stop(self) -> None:
        self._stop_event.set()
        logger.info("SparkController stopping...")

    def _run_server(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", self._port))
                s.listen(5)
                s.settimeout(1.0)

                while not self._stop_event.is_set():
                    try:
                        conn, _ = s.accept()
                        threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
                    except socket.timeout:
                        continue
                    except Exception as e:
                        if not self._stop_event.is_set():
                            logger.error(f"Spark socket server error: {e}")
            except Exception as e:
                logger.error(f"Failed to bind/listen on Spark port {self._port}: {e}")

    def _handle_client(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5.0)
            buffer = ""
            try:
                while not self._stop_event.is_set():
                    data = conn.recv(4096)
                    if not data:
                        break
                    buffer += data.decode("utf-8")

                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line.strip():
                            response = self._process_message(line)
                            if response:
                                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
            except Exception:
                pass

    def _process_message(self, message: str) -> Optional[Dict]:
        try:
            data = json.loads(message)
            msg_type = data.get("type", "heartbeat")
            pid = data.get("pid")
            app_id = data.get("spark.app.id")

            if not (pid and app_id):
                return None

            pid = int(pid)

            if msg_type == "thread_info":
                threads_array = data.get("threads", [])
                self._update_threads(pid, app_id, threads_array)
                return None # No response needed for thread info?

            # Heartbeat handling
            is_allowed = False
            with self._allowed_apps_lock:
                is_allowed = app_id in self._allowed_apps

            with self._registry_lock:
                entry = self._registry.get(pid, {"app_id": app_id, "threads": {}})
                entry["last_heartbeat"] = time.time()
                entry["app_id"] = app_id # Update in case it changed? Unlikely
                self._registry[pid] = entry

            return {"profile": is_allowed}

        except Exception as e:
            logger.warning(f"Failed to parse Spark message: {e}")
            return None

    def _update_threads(self, pid: int, app_id: str, threads_array: List[Dict]) -> None:
        with self._registry_lock:
            if pid not in self._registry:
                # Should have received heartbeat first, but just in case
                self._registry[pid] = {
                    "app_id": app_id,
                    "last_heartbeat": time.time(),
                    "threads": {}
                }

            threads_map = self._registry[pid].get("threads", {})
            for t in threads_array:
                tid = t.get("tid")
                name = t.get("name")
                if tid is not None and name:
                    threads_map[tid] = name

            self._registry[pid]["threads"] = threads_map

    def _run_backend_poller(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._client:
                    allowed = self._client.get_spark_allowed_apps()
                    with self._allowed_apps_lock:
                        self._allowed_apps = set(allowed)
            except Exception as e:
                logger.warning(f"Failed to fetch allowed Spark apps: {e}")

            self._stop_event.wait(self.BACKEND_POLL_INTERVAL_S)

    def _run_cleanup(self) -> None:
        while not self._stop_event.is_set():
            now = time.time()
            to_remove = []
            with self._registry_lock:
                for pid, info in self._registry.items():
                    if now - info["last_heartbeat"] > self.STALE_THRESHOLD_S:
                        to_remove.append(pid)

                for pid in to_remove:
                    del self._registry[pid]

            if to_remove:
                logger.info(f"Cleaned up {len(to_remove)} stale Spark processes")

            self._stop_event.wait(10)

    def filter_processes(self, processes: List[Process]) -> List[Process]:
        allowed = set()
        with self._allowed_apps_lock:
            allowed = self._allowed_apps.copy()

        with self._registry_lock:
            registry_snapshot = self._registry.copy()

        kept_processes = []
        for p in processes:
            try:
                pid = p.pid
                if pid in registry_snapshot:
                    app_id = registry_snapshot[pid]["app_id"]
                    if app_id in allowed:
                        kept_processes.append(p)
                else:
                    kept_processes.append(p)
            except Exception:
                pass

        return kept_processes
