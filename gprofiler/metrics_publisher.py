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

import logging
import platform as system_platform
import re
import socket
import time
from typing import Dict, List, Optional, Any
from urllib.parse import urlparse

from gprofiler.state import get_state


# Constants
METRIC_BASE_NAME = "gprofiler"
METRIC_ERROR_SUFFIX = ".error"
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 18126
METRIC_TYPE_COUNTER = "counter"
DEFAULT_METRIC_VALUE = 1
TIMESTAMP_MULTIPLIER = 1000  # Convert seconds to milliseconds
MAX_COMPONENT_LENGTH = 15

# Network configuration
DEFAULT_SOCKET_TIMEOUT = 5.0  # seconds
MAX_ERROR_TYPE_LENGTH = 100
MAX_ERROR_MESSAGE_LENGTH = 500
VALID_SEVERITIES = {"error", "warning", "critical", "info"}

# Fallback values
UNKNOWN_VALUE_FALLBACK = "unknown"
DEFAULT_METRIC_FALLBACK = "gprofiler_error"
PROFILER_COMPONENT_PREFIX = "profiler_"
NONE_CYCLE_FALLBACK = "none"
EMPTY_ERROR_MESSAGE_FALLBACK = "No error message provided"

# Error type constants
ERROR_TYPE_PROFILER_FAILURE = "profiler_failure"
ERROR_TYPE_PERF_FAILURE = "perf_failure"
ERROR_TYPE_PROFILING_RUN_FAILURE = "profiling_run_failure"
ERROR_TYPE_UPLOAD_TIMEOUT = "upload_timeout"
ERROR_TYPE_API_ERROR = "api_error"
ERROR_TYPE_REQUEST_EXCEPTION = "request_exception"

# Component constants
COMPONENT_SYSTEM_PROFILER = "system_profiler"
COMPONENT_API_CLIENT = "api_client"
COMPONENT_GPROFILER_MAIN = "gprofiler_main"

# Severity constants
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
SEVERITY_INFO = "info"

# Error message constants
ERROR_MSG_PROFILER_FAILURE = "profiling failed"
ERROR_MSG_PERF_FAILURE = "Running perf failed"
ERROR_MSG_PROFILING_RUN_FAILURE = "Profiling run failed"
ERROR_MSG_UPLOAD_TIMEOUT = "Upload of profile to server timed out"
ERROR_MSG_API_ERROR = "API error"
ERROR_MSG_REQUEST_EXCEPTION = "Request exception during profile upload"

# Security/robustness limits
MAX_SANITIZED_STRING_LENGTH = 200
MAX_TAG_KEY_LENGTH = 50
MAX_TAG_VALUE_LENGTH = 100
TAG_KEY_VS_GENERAL_THRESHOLD = 100  # Threshold to decide between tag key vs general string limits

# Metric categories
CATEGORY_PROFILER = "profiler"
CATEGORY_API = "api"
CATEGORY_AUTH = "auth"
CATEGORY_SYSTEM = "system"
CATEGORY_CONFIG = "config"
CATEGORY_GENERAL = "general"

# Export for external access
__all__ = [
    "MetricsHandler",
    "NoopMetricsHandler",
    "METRIC_BASE_NAME",
    "METRIC_TYPE_COUNTER",  # ← Expose the metric type constant
    "DEFAULT_METRIC_VALUE",
    # Error type constants
    "ERROR_TYPE_PROFILER_FAILURE",
    "ERROR_TYPE_PERF_FAILURE",
    "ERROR_TYPE_PROFILING_RUN_FAILURE",
    "ERROR_TYPE_UPLOAD_TIMEOUT",
    "ERROR_TYPE_API_ERROR",
    "ERROR_TYPE_REQUEST_EXCEPTION",
    # Component constants
    "COMPONENT_SYSTEM_PROFILER",
    "COMPONENT_API_CLIENT",
    "COMPONENT_GPROFILER_MAIN",
    # Severity constants
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "SEVERITY_CRITICAL",
    "SEVERITY_INFO",
    # Error message constants
    "ERROR_MSG_PROFILER_FAILURE",
    "ERROR_MSG_PERF_FAILURE",
    "ERROR_MSG_PROFILING_RUN_FAILURE",
    "ERROR_MSG_UPLOAD_TIMEOUT",
    "ERROR_MSG_API_ERROR", 
    "ERROR_MSG_REQUEST_EXCEPTION",
    # Utility functions
    "get_current_method_name",
]


def get_current_method_name() -> str:
    """
    Get the name of the calling method/function for better error context.
    
    Returns:
        Name of the calling method/function, or 'unknown_method' if not determinable
    """
    import inspect
    
    try:
        # Get the current frame
        current_frame = inspect.currentframe()
        if current_frame is None:
            return "unknown_method"
        
        # Go up the call stack:
        # Frame 0: get_current_method_name()
        # Frame 1: The send_error_metric call site
        # Frame 2: The actual method we want to identify
        caller_frame = current_frame.f_back.f_back if current_frame.f_back else None
        if caller_frame is None:
            return "unknown_method"
        
        method_name = caller_frame.f_code.co_name
        
        # Try to get class name for better context
        if "self" in caller_frame.f_locals:
            class_name = caller_frame.f_locals["self"].__class__.__name__
            return f"{class_name}.{method_name}"
        
        return method_name
        
    except Exception:
        return "unknown_method"
    finally:
        # Clean up frame references to avoid memory leaks
        del current_frame


def metric_mapping(**mappings):
    """
    Decorator that generates error_type to hierarchical metric name mappings.
    
    Usage:
        @metric_mapping(
            profiler_failure="profiler.failure",
            upload_timeout="upload.timeout", 
            api_error="api.error",
            custom_error="special.category.custom"
        )
        class MetricsHandler:
            pass
    
    This generates:
        error_mapping = {
            "profiler_failure": "gprofiler.profiler.failure",
            "upload_timeout": "gprofiler.upload.timeout",
            "api_error": "gprofiler.api.error", 
            "custom_error": "gprofiler.special.category.custom"
        }
    """
    def decorator(cls):
        # Generate the error_mapping dictionary
        error_mapping = {}
        for error_type, hierarchy in mappings.items():
            # Add base prefix and ensure proper format
            full_hierarchy = f"{METRIC_BASE_NAME}.{hierarchy}"
            error_mapping[error_type] = full_hierarchy
        
        # Add the mapping to the class
        cls._error_mapping = error_mapping
        return cls
    return decorator


@metric_mapping(
    # Profiler errors
    profiler_failure=f"{CATEGORY_PROFILER}.failure",
    perf_failure=f"{CATEGORY_PROFILER}.perf_failure", 
    profiling_run_failure=f"{CATEGORY_SYSTEM}.profiling_run_failure",
    
    # Upload/Network errors  
    upload_timeout="upload.timeout",
    api_error=f"{CATEGORY_API}.error",
    request_exception=f"{CATEGORY_API}.request_exception",
    
    # Authentication errors
    auth_failure=f"{CATEGORY_AUTH}.failure", 
    token_expired=f"{CATEGORY_AUTH}.token_expired",
    
    # System errors
    memory_error=f"{CATEGORY_SYSTEM}.memory_error",
    disk_full=f"{CATEGORY_SYSTEM}.disk_full",
    permission_denied=f"{CATEGORY_SYSTEM}.permission_denied",
    
    # Configuration errors
    config_error=f"{CATEGORY_CONFIG}.error",
    invalid_argument=f"{CATEGORY_CONFIG}.invalid_argument",
    
    # Process errors
    process_crash="process.crash",
    process_timeout="process.timeout",
)
class MetricsHandler:
    """
    Standalone metrics handler for sending error metrics to MetricAgent system.
    Uses TCP socket to connect to local metrics-agent and sends metrics in MetricAgent 'put' format.
    Can be easily enabled/disabled and used throughout the application.
    """

    def __init__(
        self,
        server_url: str,
        service_name: str,
        batch_size: int = 10,         # Unused - kept for backward compatibility  
        batch_timeout: float = 5.0,   # Unused - kept for backward compatibility
        max_queue_size: int = 1000,   # Unused - kept for backward compatibility
    ):
        """
        Initialize MetricsHandler.
        
        Args:
            server_url: URL/address for metrics-agent (e.g., "localhost:18126")
            service_name: Name of the service sending metrics
            batch_size: UNUSED - kept for backward compatibility (direct processing)
            batch_timeout: UNUSED - kept for backward compatibility (direct processing)  
            max_queue_size: UNUSED - kept for backward compatibility (direct processing)
        """
        self.server_url = server_url
        self.service_name = service_name
        self.hostname = system_platform.node()
        
        # Cache error mapping check for performance
        self._has_error_mapping = hasattr(self.__class__, '_error_mapping')
        
        # Note: batch_size, batch_timeout, max_queue_size are ignored in direct processing
        # but kept as parameters for backward compatibility with existing code
        
        # Logger for handler's own issues
        self.logger = logging.getLogger(f"{__name__}.MetricsHandler")
        self.logger.info(f"MetricAgent MetricsHandler initialized for service '{service_name}' -> {server_url}")
        self.logger.info("Using event-driven processing - direct synchronous sending")

    def send_error_metric(
        self,
        error_type: str,
        error_message: str,
        component: str,
        severity: str = "error",
        extra_tags: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Send an error metric to the metrics server.
        
        Args:
            error_type: Type of error (profiling_failure, upload_timeout, etc.)
            error_message: Human-readable error description
            component: Component where error occurred (profiler_py-spy, api_client, etc.)
            severity: Error severity (error, warning, critical)
            extra_tags: Additional tags to include
        """
        # Input validation  
        if not error_type or not error_type.strip():
            self.logger.warning("Skipping metric: error_type is empty")
            return
        
        # Use the stripped value for consistency
        error_type = error_type.strip()
            
        if len(error_type) > MAX_ERROR_TYPE_LENGTH:
            error_type = error_type[:MAX_ERROR_TYPE_LENGTH]
            self.logger.warning(f"Truncated error_type to {MAX_ERROR_TYPE_LENGTH} characters")
            
        # Validate and sanitize error_message
        if not error_message:
            error_message = EMPTY_ERROR_MESSAGE_FALLBACK
        elif len(error_message) > MAX_ERROR_MESSAGE_LENGTH:
            error_message = error_message[:MAX_ERROR_MESSAGE_LENGTH]
            self.logger.debug(f"Truncated error_message to {MAX_ERROR_MESSAGE_LENGTH} characters")
            
        if severity not in VALID_SEVERITIES:
            self.logger.warning(f"Invalid severity '{severity}', defaulting to 'error'")
            severity = "error"
        
        try:
            # Create metric in MetricAgent format
            metric = self._create_metric(
                error_type=error_type,
                error_message=error_message,
                component=component,
                severity=severity,
                extra_tags=extra_tags or {},
            )
            
            # Send directly (immediate processing)
            self._send_single_metric(metric)
            self.logger.debug(f"Sent error metric: {error_type} from {component}")
                
        except Exception as e:
            # Don't let metrics collection crash the main application
            self.logger.warning(f"Failed to send error metric '{error_type}': {e}")

    def _create_metric(
        self,
        error_type: str,
        error_message: str,
        component: str,
        severity: str,
        extra_tags: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create a metric in MetricAgent format."""
        # Create hierarchical metric name based on error type and component
        metric_name = self._build_hierarchical_metric_name(error_type, component)
        
        # Generate all tags using dedicated function
        tags = self._generate_tags(severity, component, extra_tags)
        
        # Use current time
        timestamp_seconds = time.time()
        
        # MetricAgent metric format
        return {
            "timestamp": int(timestamp_seconds * TIMESTAMP_MULTIPLIER),  # milliseconds
            "metric_name": metric_name,
            "metric_type": METRIC_TYPE_COUNTER,
            "value": DEFAULT_METRIC_VALUE,
            "message": error_message,
            "tags": tags
        }

    def _generate_tags(
        self, 
        severity: str, 
        component: str, 
        extra_tags: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Generate comprehensive tags for metrics including system, service, and gProfiler context.
        
        Args:
            severity: Error severity level (error, warning, critical)
            component: Component where error occurred (profiler_py-spy, api_client, etc.)
            extra_tags: Additional custom tags to include
            
        Returns:
            Dictionary of tags with system info, gProfiler state, and custom tags
        """
        # Base system and service tags
        tags = {
            "severity": severity,
            "hostname": self.hostname,
            "service": self.service_name,
        }
        
        # Add component-specific tags (helps with filtering and debugging)
        if component:
            tags["component"] = component
            
            # Extract profiler type from component for better categorization
            if PROFILER_COMPONENT_PREFIX in component:
                profiler_type = component.replace(PROFILER_COMPONENT_PREFIX, "").replace("-", "_")
                tags["profiler_type"] = profiler_type
        
        # Add gProfiler state information (run context)
        try:
            state = get_state()
            if state:
                tags.update({
                    "run_id": state.run_id,
                    "cycle_id": state.cycle_id or NONE_CYCLE_FALLBACK,
                })
        except Exception as e:
            # If state not available, continue without it (don't fail metric sending)
            self.logger.debug(f"Unable to retrieve gProfiler state for metrics: {e}")
        
        # Add system information for debugging
        try:
            python_version_tuple = system_platform.python_version_tuple()
            tags.update({
                "python_version": f"{python_version_tuple[0]}.{python_version_tuple[1]}",
                "os_type": system_platform.system().lower(),
            })
        except Exception as e:
            # Non-critical, continue without system info
            self.logger.debug(f"Unable to retrieve system information for metrics: {e}")
        
        # Merge extra custom tags (override defaults if same key)
        if extra_tags:
            tags.update(extra_tags)
        
        return tags

    def _send_single_metric(self, metric: Dict[str, Any]) -> None:
        """Send a single metric directly to MetricAgent (immediate processing)."""
        try:
            # Convert to MetricAgent format
            put_line = self._convert_metric_to_metric_agent_format(metric)
            self.logger.debug(f"Converted metric to MetricAgent format: {put_line}")
            
            # Send directly via TCP socket
            self._send_to_metric_agent_socket([put_line])
            
        except Exception as e:
            self.logger.warning(f"Failed to send single metric: {e}")

    @staticmethod
    def _find_category_by_patterns(text: str, patterns: Dict[str, List[str]]) -> str:
        """
        Find category by matching patterns against text.
        
        Args:
            text: Text to search patterns in
            patterns: Dictionary mapping categories to lists of patterns
            
        Returns:
            Matched category or CATEGORY_GENERAL if no match found
        """
        for category, pattern_list in patterns.items():
            if any(pattern in text for pattern in pattern_list):
                return category
        return CATEGORY_GENERAL

    def _build_hierarchical_metric_name(self, error_type: str, component: str) -> str:
        """
        Build hierarchical metric name using multiple strategies.
        
        Strategies (in order):
        1. **Explicit Mapping** (via @metric_mapping decorator) - highest priority
        2. **Convention-based parsing** (category_subcategory pattern)
        3. **Component-based** (use component as category)
        4. **Keyword-based fallback** (pattern matching)
        5. **Generic fallback** (last resort)
        
        Examples:
        - 'profiler_failure' → @metric_mapping → 'gprofiler.profiler.failure.error'
        - 'custom_timeout' → convention → 'gprofiler.custom.timeout.error'
        - 'error' + 'api_client' → component → 'gprofiler.api.error.error'
        """
        base = METRIC_BASE_NAME
        
        # Normalize input
        normalized_error = error_type.lower().replace("-", "_")
        
        # Strategy 1: Check explicit mapping first (from decorator)
        if self._has_error_mapping and normalized_error in self.__class__._error_mapping:
            base_metric = self.__class__._error_mapping[normalized_error]
            metric_name = f"{base_metric}{METRIC_ERROR_SUFFIX}"
            return self._sanitize_for_metric_agent(metric_name)
        
        # Strategy 2: Convention-based parsing (category_subcategory pattern)
        if "_" in normalized_error:
            parts = normalized_error.split("_", 1)  # Split only on first underscore
            category = parts[0]
            subcategory = parts[1] if len(parts) > 1 else ""
            
            # Build hierarchical name
            if subcategory:
                metric_name = f"{base}.{category}.{subcategory}{METRIC_ERROR_SUFFIX}"
            else:
                metric_name = f"{base}.{category}{METRIC_ERROR_SUFFIX}"
            
            return self._sanitize_for_metric_agent(metric_name)
        
        # Strategy 3: Component-based (when error_type is simple like 'timeout', 'error')
        if component:
            # Extract category from component name using pattern matching
            component_lower = component.lower()
            
            component_patterns = {
                CATEGORY_API: ["api", "client"],
                CATEGORY_PROFILER: ["profiler"],
                CATEGORY_SYSTEM: ["system"],
            }
            
            category = self._find_category_by_patterns(component_lower, component_patterns)
            
            # If no pattern matched, use component directly but clean it
            if category == CATEGORY_GENERAL:
                category = re.sub(r'[^a-zA-Z0-9_]', '', component_lower)[:MAX_COMPONENT_LENGTH]
            
            metric_name = f"{base}.{category}.{normalized_error}{METRIC_ERROR_SUFFIX}"
            return self._sanitize_for_metric_agent(metric_name)
        
        # Strategy 4: Minimal keyword-based fallback (only if above fails)
        keyword_patterns = {
            CATEGORY_PROFILER: ["prof", "perf"],
            CATEGORY_API: ["api", "upload", "request", "network"],
            CATEGORY_AUTH: ["auth", "token"],
            CATEGORY_SYSTEM: ["system", "memory", "disk"],
            CATEGORY_CONFIG: ["config", "arg", "param"],
        }
        
        category = self._find_category_by_patterns(normalized_error, keyword_patterns)
        
        metric_name = f"{base}.{category}.{normalized_error}{METRIC_ERROR_SUFFIX}"
        return self._sanitize_for_metric_agent(metric_name)

    def _sanitize_for_metric_agent(self, value: str, is_tag_value: bool = False) -> str:
        """
        Sanitize strings for MetricAgent format with length limits.
        
        Args:
            value: String to sanitize
            is_tag_value: If True, replaces invalid chars with underscore (tag values)
                         If False, keeps only valid chars (metric names and tag keys)
        
        Returns:
            Sanitized string safe for MetricAgent with appropriate length limits
        """
        if not value:
            return UNKNOWN_VALUE_FALLBACK
        
        # Convert to string and apply length limits based on usage
        value_str = str(value)
        if is_tag_value:
            value_str = value_str[:MAX_TAG_VALUE_LENGTH]
        else:
            # For metric names and tag keys
            max_length = MAX_TAG_KEY_LENGTH if len(value_str) < TAG_KEY_VS_GENERAL_THRESHOLD else MAX_SANITIZED_STRING_LENGTH
            value_str = value_str[:max_length]
        
        # MetricAgent valid characters: a-zA-Z0-9\-_./
        if is_tag_value:
            # For tag values: replace invalid characters with underscore
            sanitized = re.sub(r'[^a-zA-Z0-9\-_./]', '_', value_str)
        else:
            # For metric names and tag keys: keep only valid characters
            sanitized = re.sub(r'[^a-zA-Z0-9\-_./]', '', value_str)
        
        # Ensure we don't return empty strings
        return sanitized if sanitized else UNKNOWN_VALUE_FALLBACK

    def _convert_metric_to_metric_agent_format(self, metric: Dict[str, Any]) -> str:
        """
        Convert a metric dict to MetricAgent 'put' format string.
        
        Format: 'put name epoch value tag=value tag=value tag=value'
        """
        # Extract basic info
        metric_name = self._sanitize_for_metric_agent(metric.get("metric_name", DEFAULT_METRIC_FALLBACK))
        
        # Convert timestamp from milliseconds to seconds (no decimal)
        timestamp_ms = metric.get("timestamp", int(time.time() * TIMESTAMP_MULTIPLIER))
        epoch_seconds = int(timestamp_ms / TIMESTAMP_MULTIPLIER)  # Convert to seconds, no decimal
        
        value = metric.get("value", DEFAULT_METRIC_VALUE)
        
        # Build tags
        tags = []
        metric_tags = metric.get("tags", {})
        
        # Add standard tags with validation
        for tag_key, tag_value in metric_tags.items():
            # Skip None or empty tag keys/values
            if tag_key is None or tag_value is None:
                continue
                
            clean_key = self._sanitize_for_metric_agent(tag_key, is_tag_value=False)
            clean_value = self._sanitize_for_metric_agent(str(tag_value), is_tag_value=True)
            
            # Only add if both key and value are valid and not just fallback values
            if (clean_key and clean_value and 
                clean_key != UNKNOWN_VALUE_FALLBACK and 
                clean_value != UNKNOWN_VALUE_FALLBACK):
                tags.append(f"{clean_key}={clean_value}")
        
        # Build the put format string
        tag_string = " ".join(tags)
        put_line = f"put {metric_name} {epoch_seconds} {value}"
        if tag_string:
            put_line += f" {tag_string}"
        
        return put_line

    def _send_to_metric_agent_socket(self, metric_lines: List[str]) -> None:
        """Send metric lines to MetricAgent via TCP socket."""
        # Parse server_url to extract host and port
        host = DEFAULT_HOST  # Default fallback
        port = DEFAULT_PORT  # Default fallback
        
        if self.server_url:
            try:
                # Handle different URL formats:
                # - "localhost:18126" (simple host:port)
                # - "ws://localhost:18126" (WebSocket URL)
                # - "tcp://localhost:18126" (TCP URL)
                # - "localhost" (host only)
                
                if '://' in self.server_url:
                    # URL format (ws://, tcp://, etc.)
                    parsed = urlparse(self.server_url)
                    if parsed.hostname:
                        host = parsed.hostname
                    if parsed.port:
                        port = parsed.port
                else:
                    # Simple host:port format
                    if ':' in self.server_url:
                        host_part, port_part = self.server_url.rsplit(':', 1)
                        if host_part:
                            host = host_part
                        if port_part.isdigit():
                            port = int(port_part)
                    else:
                        # Just hostname provided, use default port
                        host = self.server_url
                        
            except Exception as e:
                self.logger.warning(f"Failed to parse server_url '{self.server_url}': {e}. Using defaults {host}:{port}")
        
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(DEFAULT_SOCKET_TIMEOUT)  # Prevent hanging
            sock.connect((host, port))
            
            # Metrics must end with newline for each metric sent!
            metrics_data = '\n'.join(metric_lines) + '\n'
            sock.sendall(metrics_data.encode('utf-8'))
            
            self.logger.debug(f"Successfully sent {len(metric_lines)} metrics to MetricAgent at {host}:{port}")
            
        except ConnectionRefusedError:
            self.logger.warning(f"Connection refused - metrics-agent not running on {host}:{port}")
        except socket.timeout:
            self.logger.warning(f"Socket timeout ({DEFAULT_SOCKET_TIMEOUT}s) connecting to {host}:{port}")
        except OSError as e:
            self.logger.warning(f"Network error sending to MetricAgent at {host}:{port}: {e}")
        except Exception as e:
            self.logger.warning(f"Unexpected error sending to MetricAgent at {host}:{port}: {e}")
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass  # Ignore cleanup errors

    def flush_and_close(self) -> None:
        """Cleanup handler (direct processing has no background resources to clean up)."""
        self.logger.info("MetricsHandler shutdown complete - direct processing requires no cleanup")


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
