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
        # Map PID -> {"app_id": str, "last_heartbeat": float}
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
        # Connect to self to unblock accept() if needed, or just let daemon threads die.
        # Since we use setDaemon(True), we can just let them die when main process exits,
        # but for clean shutdown we might want to close the socket.
        # For now, rely on daemon threads.
        logger.info("SparkController stopping...")

    def _run_server(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", self._port))
                s.listen(5)
                s.settimeout(1.0)  # Check stop event every second

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
                    data = conn.recv(1024)
                    if not data:
                        break
                    buffer += data.decode("utf-8")

                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line.strip():
                            self._process_message(line)
            except Exception:
                pass

    def _process_message(self, message: str) -> None:
        try:
            data = json.loads(message)
            pid = data.get("pid")
            app_id = data.get("spark.app.id")

            if pid and app_id:
                with self._registry_lock:
                    self._registry[int(pid)] = {
                        "app_id": app_id,
                        "last_heartbeat": time.time()
                    }
                    # logger.debug(f"Received Spark heartbeat: PID={pid}, AppID={app_id}")
        except Exception as e:
            logger.warning(f"Failed to parse Spark heartbeat: {e}")

    def _run_backend_poller(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._client:
                    allowed = self._client.get_spark_allowed_apps()
                    with self._allowed_apps_lock:
                        self._allowed_apps = set(allowed)
                    # logger.debug(f"Updated allowed Spark apps: {self._allowed_apps}")
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
        """
        Filter the list of processes.
        If a process is identified as a Spark process (exists in registry),
        it is ONLY kept if its App ID is in the allowed list.
        Non-Spark processes are kept as is.
        """
        allowed = set()
        with self._allowed_apps_lock:
            allowed = self._allowed_apps.copy()

        # We need a snapshot of registry to avoid holding lock too long
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
                    # else: identified as Spark but not allowed -> drop
                else:
                    # Not a known Spark process -> keep it (gProfiler default behavior)
                    kept_processes.append(p)
            except Exception:
                # If checking PID fails, safe to ignore
                pass

        return kept_processes
