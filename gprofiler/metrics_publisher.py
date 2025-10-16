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
import platform as system_platform
import queue
import re
import socket
import threading
import time
from typing import Dict, Optional, Any
import websocket  # websocket-client package

from gprofiler.state import get_state


class MetricsHandler:
    """
    Standalone metrics handler for sending error metrics to Pinterest's Goku system.
    Uses TCP socket to connect to local metrics-agent and sends metrics in Goku 'put' format.
    Can be easily enabled/disabled and used throughout the application.
    """

    def __init__(
        self,
        server_url: str,
        service_name: str,
        batch_size: int = 10,
        batch_timeout: float = 5.0,
        max_queue_size: int = 1000,
    ):
        self.server_url = server_url
        self.service_name = service_name
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout
        self.hostname = system_platform.node()
        
        # Metrics queue for batching
        self.metrics_queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        
        # WebSocket connection (lazy initialization)
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # Metrics sender thread
        self._sender_thread = threading.Thread(target=self._metrics_sender_loop, daemon=True)
        self._sender_thread.start()
        
        # Logger for handler's own issues
        self.logger = logging.getLogger(f"{__name__}.MetricsHandler")
        self.logger.info(f"Goku MetricsHandler initialized for service '{service_name}' -> {server_url}")

    def send_error_metric(
        self,
        error_type: str,
        error_message: str,
        component: str,
        severity: str = "error",
        extra_tags: Optional[Dict[str, Any]] = None,
        error_timestamp: Optional[float] = None,
    ) -> None:
        """
        Send an error metric to the metrics server.
        
        Args:
            error_type: Type of error (profiling_failure, upload_timeout, etc.)
            error_message: Human-readable error description
            component: Component where error occurred (profiler_py-spy, api_client, etc.)
            severity: Error severity (error, warning, critical)
            extra_tags: Additional tags to include
            error_timestamp: When the error actually occurred (seconds since epoch). If None, uses current time.
        """
        try:
            # Create metric in company-specific format
            metric = self._create_metric(
                error_type=error_type,
                error_message=error_message,
                component=component,
                severity=severity,
                extra_tags=extra_tags or {},
                error_timestamp=error_timestamp
            )
            
            # Queue for batched sending
            try:
                self.metrics_queue.put_nowait(metric)
                self.logger.debug(f"Queued error metric: {error_type} from {component}")
            except queue.Full:
                self.logger.warning("Metrics queue is full, dropping metric")
                
        except Exception as e:
            # Don't let metrics collection crash the main application
            self.logger.warning(f"Failed to create error metric: {e}")

    def _create_metric(
        self,
        error_type: str,
        error_message: str,
        component: str,
        severity: str,
        extra_tags: Dict[str, Any],
        error_timestamp: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Create a metric in company-specific format."""
        # Create hierarchical metric name based on error type and component
        metric_name = self._build_hierarchical_metric_name(error_type, component, severity)
        
        # Get gProfiler state information if available
        tags = {
            "severity": severity,
            "hostname": self.hostname,
            "service": self.service_name,
        }
        
        # Add component-specific tags (remove from metric name to avoid duplication)
        if component:
            tags["component"] = component
        
        # Add gProfiler state information
        try:
            state = get_state()
            tags.update({
                "run_id": state.run_id,
                "cycle_id": state.cycle_id or "none",
            })
        except Exception:
            # If state not available, continue without it
            pass
        
        # Add extra tags
        tags.update(extra_tags)
        
        # Use provided timestamp or current time
        timestamp_seconds = error_timestamp if error_timestamp is not None else time.time()
        
        # Company-specific metric format
        return {
            "timestamp": int(timestamp_seconds * 1000),  # milliseconds
            "metric_name": metric_name,
            "metric_type": "counter",
            "value": 1,
            "message": error_message,
            "tags": tags
        }

    def _build_hierarchical_metric_name(self, error_type: str, component: str, severity: str) -> str:
        """
        Build hierarchical metric names with consistent .error suffix for better filtering.
        
        Examples:
            profiler_failure + profiler_py-spy → gprofiler.profiler.error  
            upload_timeout + api_client → gprofiler.upload.timeout.error
            api_error + api_client → gprofiler.api.error
            perf_failure + profiler_perf → gprofiler.profiler.perf_failure.error
            
        All metrics end with .error for consistent wildcard filtering: *.error
        """
        # Base prefix
        base = "gprofiler"
        
        # Map error types to hierarchical categories (without .error suffix yet)
        error_mapping = {
            # Profiler errors
            "profiler_failure": f"{base}.profiler",
            "perf_failure": f"{base}.profiler.perf_failure", 
            "profiling_run_failure": f"{base}.system.profiling_run_failure",
            
            # Upload/Network errors
            "upload_timeout": f"{base}.upload.timeout",
            "api_error": f"{base}.api",
            "request_exception": f"{base}.api.request_exception",
            
            # Authentication errors
            "auth_failure": f"{base}.auth.failure",
            "token_expired": f"{base}.auth.token_expired",
            
            # System errors  
            "memory_error": f"{base}.system.memory_error",
            "disk_full": f"{base}.system.disk_full",
            "permission_denied": f"{base}.system.permission_denied",
            
            # Configuration errors
            "config_error": f"{base}.config",
            "invalid_argument": f"{base}.config.invalid_argument",
            
            # Process errors
            "process_crash": f"{base}.process.crash",
            "process_timeout": f"{base}.process.timeout",
        }
        
        # Try to find exact match first
        if error_type in error_mapping:
            base_metric = error_mapping[error_type]
        else:
            # Fallback: create category based on error_type patterns
            if "profiler" in error_type.lower() or "perf" in error_type.lower():
                category = "profiler"
            elif "upload" in error_type.lower() or "api" in error_type.lower() or "request" in error_type.lower():
                category = "api"
            elif "auth" in error_type.lower() or "token" in error_type.lower():
                category = "auth"
            elif "system" in error_type.lower() or "memory" in error_type.lower() or "disk" in error_type.lower():
                category = "system"
            elif "config" in error_type.lower() or "argument" in error_type.lower():
                category = "config"
            elif "process" in error_type.lower():
                category = "process"
            else:
                category = "general"
            
            # Clean the error type for the metric name
            clean_error = error_type.lower().replace("_", "").replace("-", "")
            
            # Build base hierarchical name
            base_metric = f"{base}.{category}.{clean_error}"
        
        # Always add .error suffix for consistent filtering
        metric_name = f"{base_metric}.error"
        
        return self._sanitize_for_goku(metric_name)

    def _metrics_sender_loop(self) -> None:
        """Main loop for batching and sending metrics."""
        batch = []
        last_send_time = time.time()
        
        while not self._stop_event.is_set():
            try:
                # Try to get metrics from queue
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
                    self._send_metrics_batch(batch.copy())
                    batch.clear()
                    last_send_time = current_time
                    
            except Exception as e:
                self.logger.warning(f"Error in metrics sender loop: {e}")

    def _sanitize_for_goku(self, value: str, is_tag_value: bool = False) -> str:
        """
        Sanitize strings for Goku format.
        
        Args:
            value: String to sanitize
            is_tag_value: If True, replaces invalid chars with underscore (tag values)
                         If False, keeps only valid chars (metric names and tag keys)
        
        Returns:
            Sanitized string safe for Goku
        """
        if not value:
            return "unknown"
        
        # Goku valid characters: a-zA-Z0-9\-_./
        if is_tag_value:
            # For tag values: replace invalid characters with underscore
            return re.sub(r'[^a-zA-Z0-9\-_./]', '_', str(value))
        else:
            # For metric names and tag keys: keep only valid characters
            return re.sub(r'[^a-zA-Z0-9\-_./]', '', str(value))

    def _convert_metric_to_goku_format(self, metric: Dict[str, Any]) -> str:
        """
        Convert a metric dict to Goku 'put' format string.
        
        Format: 'put name epoch value tag=value tag=value tag=value'
        """
        # Extract basic info
        metric_name = self._sanitize_for_goku(metric.get("metric_name", "gprofiler_error"))
        
        # Convert timestamp from milliseconds to seconds (no decimal)
        timestamp_ms = metric.get("timestamp", int(time.time() * 1000))
        epoch_seconds = int(timestamp_ms / 1000)  # Convert to seconds, no decimal
        
        value = metric.get("value", 1)
        
        # Build tags
        tags = []
        metric_tags = metric.get("tags", {})
        
        # Add standard tags
        for tag_key, tag_value in metric_tags.items():
            clean_key = self._sanitize_for_goku(tag_key, is_tag_value=False)
            clean_value = self._sanitize_for_goku(str(tag_value), is_tag_value=True)
            if clean_key and clean_value:  # Only add if both key and value are valid
                tags.append(f"{clean_key}={clean_value}")
        
        # Build the put format string
        tag_string = " ".join(tags)
        put_line = f"put {metric_name} {epoch_seconds} {value}"
        if tag_string:
            put_line += f" {tag_string}"
        
        return put_line

    def _send_metrics_batch(self, metrics_batch: list) -> None:
        """Send a batch of metrics to Goku via TCP socket in 'put' format."""
        try:
            # Convert metrics to Goku format
            goku_lines = []
            for metric in metrics_batch:
                try:
                    put_line = self._convert_metric_to_goku_format(metric)
                    goku_lines.append(put_line)
                    self.logger.debug(f"Converted metric to Goku format: {put_line}")
                except Exception as e:
                    self.logger.warning(f"Failed to convert metric to Goku format: {e}")
                    continue
            
            if not goku_lines:
                self.logger.warning("No valid metrics to send to Goku")
                return
            
            # Send to Goku via TCP socket
            self._send_to_goku_socket(goku_lines)
            
        except Exception as e:
            self.logger.warning(f"Failed to send metrics batch to Goku: {e}")

    def _send_to_goku_socket(self, metric_lines: list) -> None:
        """Send metric lines to Goku via TCP socket."""
        # Extract host and port from server_url if it's a WebSocket URL
        # For backward compatibility, parse WebSocket URLs to extract host/port
        host = 'localhost'
        port = 18126
        
        if self.server_url:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(self.server_url)
                if parsed.hostname:
                    host = parsed.hostname
                if parsed.port:
                    port = parsed.port
            except Exception:
                # If parsing fails, use defaults
                pass
        
        sock = None
        try:
            sock = socket.socket()
            sock.connect((host, port))
            
            # Metrics must end with newline for each metric sent!
            metrics_data = '\n'.join(metric_lines) + '\n'
            sock.sendall(metrics_data.encode('utf-8'))
            
            self.logger.debug(f"Successfully sent {len(metric_lines)} metrics to Goku at {host}:{port}")
            
        except ConnectionRefusedError:
            self.logger.warning(f"Connection refused - metrics-agent not running on {host}:{port}")
        except Exception as e:
            self.logger.warning(f"Failed to send metrics to Goku at {host}:{port}: {e}")
        finally:
            if sock:
                sock.close()

    def _ensure_websocket_connection(self) -> bool:
        """Ensure WebSocket connection is established."""
        if self._ws and self._ws.sock and self._ws.sock.connected:
            return True
        
        try:
            self.logger.info(f"Establishing WebSocket connection to {self.server_url}")
            
            self._ws = websocket.WebSocketApp(
                self.server_url,
                on_open=self._on_websocket_open,
                on_error=self._on_websocket_error,
                on_close=self._on_websocket_close,
                on_message=self._on_websocket_message
            )
            
            # Start WebSocket in separate thread
            if not self._ws_thread or not self._ws_thread.is_alive():
                self._ws_thread = threading.Thread(
                    target=self._ws.run_forever,
                    daemon=True
                )
                self._ws_thread.start()
                
                # Give it a moment to connect
                time.sleep(0.5)
            
            return self._ws.sock and self._ws.sock.connected
            
        except Exception as e:
            self.logger.warning(f"Failed to establish WebSocket connection: {e}")
            return False

    def _on_websocket_open(self, ws) -> None:
        """Called when WebSocket connection is opened."""
        self.logger.info("WebSocket connection established")

    def _on_websocket_error(self, ws, error) -> None:
        """Called when WebSocket encounters an error."""
        self.logger.warning(f"WebSocket error: {error}")

    def _on_websocket_close(self, ws, close_status_code, close_msg) -> None:
        """Called when WebSocket connection is closed."""
        self.logger.info(f"WebSocket connection closed: {close_status_code} - {close_msg}")

    def _on_websocket_message(self, ws, message) -> None:
        """Called when WebSocket receives a message."""
        self.logger.debug(f"Received WebSocket message: {message}")

    def flush_and_close(self) -> None:
        """Flush remaining metrics and close connections."""
        self.logger.info("Shutting down MetricsHandler...")
        
        # Stop sender thread
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
            self.logger.info(f"Flushed {len(remaining_metrics)} remaining metrics")
        
        # Close WebSocket
        if self._ws:
            self._ws.close()
        
        # Wait for threads to finish
        if self._sender_thread and self._sender_thread.is_alive():
            self._sender_thread.join(timeout=2.0)
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=2.0)
        
        self.logger.info("MetricsHandler shutdown complete")


class NoopMetricsHandler:
    """
    No-operation metrics handler for when metrics are disabled.
    Provides the same interface but does nothing.
    """
    
    def __init__(self, *args, **kwargs):
        pass
    
    def send_error_metric(self, *args, **kwargs) -> None:
        pass
    
    def flush_and_close(self) -> None:
        pass
