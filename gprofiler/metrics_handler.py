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
import json
import logging
import queue
import threading
import time
import websocket  # websocket-client package
from logging import LogRecord
from typing import Dict, Optional, Any
from urllib.parse import urlparse

from gprofiler.state import get_state


class ErrorMetricsHandler(logging.Handler):
    """
    A logging handler that sends error metrics to a websocket-based metrics service.
    Captures ERROR and CRITICAL level logs and converts them to metrics.
    """

    def __init__(
        self,
        metrics_server_url: str,
        service_name: str,
        auth_token: Optional[str] = None,
        max_queue_size: int = 1000,
        batch_size: int = 10,
        batch_timeout: float = 5.0,
    ):
        super().__init__()
        self.metrics_server_url = metrics_server_url
        self.service_name = service_name
        self.auth_token = auth_token
        self.max_queue_size = max_queue_size
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout
        
        # Queue for metrics to be sent
        self.metrics_queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        
        # WebSocket connection (lazy initialization)
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # Start the metrics sender thread
        self._sender_thread = threading.Thread(target=self._metrics_sender_loop, daemon=True)
        self._sender_thread.start()
        
        # Only handle ERROR and CRITICAL levels
        self.setLevel(logging.ERROR)

    def emit(self, record: LogRecord) -> None:
        """Convert log record to metric and queue it for sending."""
        try:
            metric = self._create_metric_from_record(record)
            if metric:
                # Non-blocking put - if queue is full, drop the metric
                try:
                    self.metrics_queue.put_nowait(metric)
                except queue.Full:
                    # Could log this but avoid infinite recursion
                    pass
        except Exception:
            # Avoid infinite recursion if this handler itself fails
            pass

    def _create_metric_from_record(self, record: LogRecord) -> Optional[Dict[str, Any]]:
        """Convert a log record to a metric in your company's format."""
        if record.levelno < logging.ERROR:
            return None
            
        state = get_state()
        
        # Extract error category from the logger name or message
        error_category = self._categorize_error(record)
        
        # Create metric in your company's format
        metric = {
            "timestamp": int(time.time() * 1000),  # milliseconds
            "service": self.service_name,
            "metric_name": "gprofiler_error_count",
            "metric_type": "counter",
            "value": 1,
            "tags": {
                "error_level": record.levelname.lower(),
                "error_category": error_category,
                "logger_name": record.name,
                "gprofiler_version": getattr(record, 'gprofiler_version', 'unknown'),
                "run_id": state.run_id,
                "cycle_id": state.cycle_id or "none",
            }
        }
        
        # Add exception information if available
        if record.exc_info:
            exc_type = record.exc_info[0].__name__ if record.exc_info[0] else "unknown"
            metric["tags"]["exception_type"] = exc_type
            
        return metric

    def _categorize_error(self, record: LogRecord) -> str:
        """Categorize errors based on logger name and message content."""
        logger_name = record.name.lower()
        message = record.getMessage().lower()
        
        # Categorize based on your application's structure
        if "profiler" in logger_name or "profiling" in message:
            return "profiler_error"
        elif "upload" in message or "server" in message or "api" in message:
            return "upload_error"
        elif "memory" in message or "cleanup" in message:
            return "memory_error"
        elif "system" in logger_name or "metrics" in logger_name:
            return "system_error"
        elif "container" in message or "docker" in message:
            return "container_error"
        else:
            return "general_error"

    def _metrics_sender_loop(self) -> None:
        """Main loop for batching and sending metrics."""
        batch = []
        last_send_time = time.time()
        
        while not self._stop_event.is_set():
            try:
                # Try to get a metric with timeout
                try:
                    metric = self.metrics_queue.get(timeout=1.0)
                    batch.append(metric)
                except queue.Empty:
                    pass
                
                current_time = time.time()
                should_send = (
                    len(batch) >= self.batch_size or 
                    (batch and current_time - last_send_time >= self.batch_timeout)
                )
                
                if should_send and batch:
                    self._send_metrics_batch(batch)
                    batch.clear()
                    last_send_time = current_time
                    
            except Exception:
                # Avoid infinite recursion - could implement dead letter queue here
                pass

    def _send_metrics_batch(self, metrics_batch: list) -> None:
        """Send a batch of metrics to the websocket service."""
        try:
            if not self._ensure_websocket_connection():
                return
                
            # Format according to your company's protocol
            message = {
                "type": "metrics",
                "batch": metrics_batch,
                "auth_token": self.auth_token
            }
            
            if self._ws:
                self._ws.send(json.dumps(message))
                
        except Exception:
            # Could implement retry logic or dead letter queue here
            pass

    def _ensure_websocket_connection(self) -> bool:
        """Ensure websocket connection is established."""
        if self._ws and self._ws.sock and self._ws.sock.connected:
            return True
            
        try:
            self._ws = websocket.WebSocketApp(
                self.metrics_server_url,
                on_open=self._on_websocket_open,
                on_error=self._on_websocket_error,
                on_close=self._on_websocket_close
            )
            
            # Start websocket in a separate thread if not already running
            if not self._ws_thread or not self._ws_thread.is_alive():
                self._ws_thread = threading.Thread(
                    target=self._ws.run_forever,
                    daemon=True
                )
                self._ws_thread.start()
                
                # Give it a moment to connect
                time.sleep(0.5)
                
            return self._ws.sock and self._ws.sock.connected
            
        except Exception:
            return False

    def _on_websocket_open(self, ws) -> None:
        """Called when websocket connection is opened."""
        pass

    def _on_websocket_error(self, ws, error) -> None:
        """Called when websocket encounters an error."""
        pass

    def _on_websocket_close(self, ws, close_status_code, close_msg) -> None:
        """Called when websocket connection is closed."""
        pass

    def close(self) -> None:
        """Clean shutdown of the handler."""
        self._stop_event.set()
        
        # Send any remaining metrics
        remaining_metrics = []
        while not self.metrics_queue.empty():
            try:
                remaining_metrics.append(self.metrics_queue.get_nowait())
            except queue.Empty:
                break
                
        if remaining_metrics:
            self._send_metrics_batch(remaining_metrics)
            
        # Close websocket
        if self._ws:
            self._ws.close()
            
        # Wait for threads to finish
        if self._sender_thread and self._sender_thread.is_alive():
            self._sender_thread.join(timeout=2.0)
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=2.0)