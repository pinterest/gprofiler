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

import datetime
import logging
import socket
import threading
from pathlib import Path
from typing import Dict, Any, Optional, List, TYPE_CHECKING

import configargparse
import requests
from psutil import Process

# Use TYPE_CHECKING to avoid circular imports
if TYPE_CHECKING:
    from gprofiler.main import GProfiler

from gprofiler.client import ProfilerAPIClient
from gprofiler.containers_client import ContainerNamesClient
from gprofiler.metadata.application_identifiers import ApplicationIdentifiers
from gprofiler.metadata.enrichment import EnrichmentOptions
from gprofiler.metadata.metadata_collector import get_static_metadata
from gprofiler.metadata.system_metadata import get_hostname
from gprofiler.profiler_state import ProfilerState
from gprofiler.profilers.factory import get_profilers
from gprofiler.profilers.profiler_base import NoopProfiler
from gprofiler.profilers.registry import get_profilers_registry
from gprofiler.state import State, init_state, get_state
from gprofiler.system_metrics import NoopSystemMetricsMonitor, SystemMetricsMonitor, SystemMetricsMonitorBase
from gprofiler.usage_loggers import NoopUsageLogger
from gprofiler.utils import TEMPORARY_STORAGE_PATH, pgrep_maps, pgrep_exe
from gprofiler.hw_metrics import HWMetricsMonitor, HWMetricsMonitorBase, NoopHWMetricsMonitor
from gprofiler.exceptions import NoProfilersEnabledError

logger = logging.getLogger(__name__)


class HeartbeatClient:
    """Client for sending heartbeats to the server and receiving profiling commands"""
    
    def __init__(self, api_server: str, service_name: str, server_token: str, verify: bool = True):
        self.api_server = api_server.rstrip('/')
        self.service_name = service_name
        self.server_token = server_token
        self.verify = verify
        self.hostname = get_hostname()
        self.ip_address = self._get_local_ip()
        self.last_command_id: Optional[str] = None
        self.executed_command_ids: set = set()  # Track executed command IDs for idempotency (in-memory)
        self.max_command_history = 1000  # Limit command history to prevent memory growth
        self.session = requests.Session()
        
        # Set up authentication headers
        if self.server_token:
            self.session.headers.update({
                'Authorization': f'Bearer {self.server_token}',
                'Content-Type': 'application/json'
            })
    
    def _get_local_ip(self) -> str:
        """Get the local IP address"""
        try:
            # Connect to a remote address to determine local IP
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
    
    def send_heartbeat(
        self,
        available_pids: Optional[Dict[str, List[int]]] = None
    ) -> Optional[Dict[str, Any]]:
        """Send heartbeat to server and return any profiling commands"""
        try:
            heartbeat_data = {
                "ip_address": self.ip_address,
                "hostname": self.hostname,
                "service_name": self.service_name,
                "last_command_id": self.last_command_id,
                "status": "active",
                "timestamp": datetime.datetime.now().isoformat()
            }
            
            # Include available PIDs if provided
            if available_pids is not None:
                heartbeat_data["available_pids"] = available_pids
            
            url = f"{self.api_server}/api/metrics/heartbeat"
            response = self.session.post(
                url,
                json=heartbeat_data,
                verify=self.verify,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("success") and result.get("profiling_command"):
                    logger.info(f"Received profiling command from server: {result.get('command_id')}")
                    return result
                else:
                    logger.debug("Heartbeat successful, no pending commands")
                    return None
            else:
                logger.warning(f"Heartbeat failed with status {response.status_code}: {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to send heartbeat: {e}")
            return None
    
    def send_command_completion(self, command_id: str, status: str, execution_time: Optional[int] = None, 
                               error_message: Optional[str] = None, results_path: Optional[str] = None) -> bool:
        """
        Send command completion status to the server.
        
        Args:
            command_id: The ID of the completed command
            status: 'completed' or 'failed'
            execution_time: Duration of execution in seconds
            error_message: Error message if status is 'failed'
            results_path: Path to profiling results if available
            
        Returns:
            bool: True if completion was successfully reported, False otherwise
        """
        try:
            completion_data = {
                "command_id": command_id,
                "hostname": self.hostname,
                "status": status,
                "execution_time": execution_time,
                "error_message": error_message,
                "results_path": results_path
            }
            
            url = f"{self.api_server}/api/metrics/command_completion"
            response = self.session.post(
                url,
                json=completion_data,
                verify=self.verify,
                timeout=30
            )
            
            if response.status_code == 200:
                logger.info(f"Successfully reported command completion for {command_id} with status: {status}")
                return True
            else:
                logger.error(f"Failed to report command completion for {command_id}. Status: {response.status_code}, Response: {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to send command completion for {command_id}: {e}")
            return False

    
    
    def mark_command_executed(self, command_id: str):
        """Mark a command as executed (in-memory)"""
        self.executed_command_ids.add(command_id)
        
        # Cleanup old command IDs if we exceed the limit
        if len(self.executed_command_ids) > self.max_command_history:
            self._cleanup_old_command_ids()
        
        logger.debug(f"Marked command ID {command_id} as executed")
    
    def _cleanup_old_command_ids(self):
        """Remove old command IDs to prevent memory growth"""
        try:
            # Keep only the most recent commands (this is a simple approach)
            # In production, you might want to implement time-based cleanup
            if len(self.executed_command_ids) > self.max_command_history:
                # Convert to list, sort, and keep the last max_command_history items
                command_list = list(self.executed_command_ids)
                # Since UUIDs don't sort chronologically, we'll just remove some arbitrary ones
                # In a real implementation, you'd want to track timestamps
                commands_to_keep = command_list[-self.max_command_history:]
                self.executed_command_ids = set(commands_to_keep)
                logger.info(f"Cleaned up command ID history in memory, keeping {len(self.executed_command_ids)} entries")
        except Exception as e:
            logger.warning(f"Failed to cleanup old command IDs: {e}")


class DynamicGProfilerManager:
    """Manager for dynamically starting/stopping gProfiler instances based on server commands"""
    
    def __init__(self, base_args: configargparse.Namespace, heartbeat_client: HeartbeatClient):
        self.base_args = base_args
        self.heartbeat_client = heartbeat_client
        self.current_gprofiler: Optional['GProfiler'] = None
        self.current_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.heartbeat_interval = 30  # seconds
    
    def scan_available_pids(self) -> Dict[str, List[int]]:
        """Scan the host for all available PIDs organized by language/profiler type"""
        available_pids = {}
        
        try:
            # Import regex patterns from granulate_utils
            from granulate_utils.java import DETECTED_JAVA_PROCESSES_REGEX
            from granulate_utils.python import DETECTED_PYTHON_PROCESSES_REGEX
            
            # Define profiler patterns
            profiler_patterns = {
                'java': DETECTED_JAVA_PROCESSES_REGEX,
                'python': DETECTED_PYTHON_PROCESSES_REGEX,
                'ruby': r"^.+/libruby",  # Ruby pattern
                'dotnet': r"^.+/libcoreclr\.so",  # .NET pattern
                'php': r"^.+/(lib)?php[^/]*$",  # PHP pattern
            }
            
            # Scan for each language
            for language, pattern in profiler_patterns.items():
                try:
                    if language == 'python':
                        # Python uses both pgrep_maps and pgrep_exe on Windows
                        from gprofiler.platform import is_windows
                        if is_windows():
                            processes = pgrep_exe("python")
                        else:
                            processes = pgrep_maps(pattern)
                    else:
                        # Other languages use pgrep_maps
                        processes = pgrep_maps(pattern)
                    
                    pids = [p.pid for p in processes]
                    
                    # Only include languages that have running processes
                    if pids:
                        available_pids[language] = sorted(pids)
                        logger.debug(f"Found {len(pids)} {language} processes")
                    
                except Exception as e:
                    # Don't let individual profiler errors break the entire scan
                    logger.debug(f"Error scanning {language} processes: {e}")
                    continue
            
            if available_pids:
                total_pids = sum(len(pids) for pids in available_pids.values())
                logger.debug(
                    f"Total available PIDs found: {total_pids} processes across "
                    f"{len(available_pids)} languages"
                )
            else:
                logger.debug("No available PIDs found for any supported languages")
            
        except Exception as e:
            logger.debug(f"Error scanning available PIDs: {e}")
        
        return available_pids
    
    def start_heartbeat_loop(self):
        """Start the main heartbeat loop"""
        logger.info("Starting heartbeat loop...")
        
        while not self.stop_event.is_set():
            try:
                # Scan all available PIDs by language
                available_pids = self.scan_available_pids()
                if available_pids:
                    total_pids = sum(len(pids) for pids in available_pids.values())
                    logger.debug(
                        f"Sending heartbeat with {total_pids} PIDs across "
                        f"{len(available_pids)} languages"
                    )
                
                # Send heartbeat and check for commands
                command_response = self.heartbeat_client.send_heartbeat(
                    available_pids=available_pids
                )
                
                if command_response and command_response.get("profiling_command"):
                    profiling_command = command_response["profiling_command"]
                    command_id = command_response["command_id"]
                    command_type = profiling_command.get("command_type", "start")
                    
                    logger.info(f"Received profiling command: {profiling_command}")
                    
                    # Check for idempotency - skip if command already executed
                    if command_id in self.heartbeat_client.executed_command_ids:
                        logger.info(f"Command ID {command_id} already executed, skipping...")

                        # Wait for next heartbeat
                        self.stop_event.wait(self.heartbeat_interval)

                        continue
                    
                    logger.info(f"Received {command_type} command: {command_id}")
                    
                    # Mark command as executed for idempotency
                    self.heartbeat_client.mark_command_executed(command_id)
                    self.heartbeat_client.last_command_id = command_id
                    
                    if command_type == "stop":
                        # Stop current profiler without starting a new one
                        logger.info(f"RECEIVED STOP COMMAND for command ID: {command_id}")
                        logger.info(f"STOP command details: {profiling_command}")
                        self._stop_current_profiler()
                        # TODO: important comment to make sure profiler has stopped successful to avoid leak 
                        # Report completion for stop command
                        self.heartbeat_client.send_command_completion(
                            command_id=command_id,
                            status="completed",
                            execution_time=0,
                            error_message=None,
                            results_path=None
                        )
                    elif command_type == "start":
                        # Stop current profiler if running, then start new one
                        logger.info("Starting new profiler due to start command")
                        # TODO: important comment to make sure profiler has stopped successful to avoid leak 
                        self._stop_current_profiler()
                        self._start_new_profiler(profiling_command, command_id)
                        # Note: command completion still needs since it will wait for successful profiling 
                        # Report command completion to the server
                        try:
                            self.heartbeat_client.send_command_completion(
                                command_id=command_id,
                                status="completed",
                                execution_time=0,
                                error_message=None,
                                results_path=None
                            )
                        except Exception as e:
                            logger.error(f"Failed to report command completion for {command_id}: {e}")
                    else:
                        logger.warning(f"Unknown command type: {command_type}")
                        # Report completion for unknown command type
                        self.heartbeat_client.send_command_completion(
                            command_id=command_id,
                            status="failed",
                            execution_time=0,
                            error_message=f"Unknown command type: {command_type}",
                            results_path=None
                        )
                
                
                # Wait for next heartbeat
                self.stop_event.wait(self.heartbeat_interval)
                
            except Exception as e:
                logger.error(f"Error in heartbeat loop: {e}", exc_info=True)
                self.stop_event.wait(self.heartbeat_interval)
    
    def _stop_current_profiler(self):
        """Stop the currently running profiler"""
        if self.current_gprofiler:
            logger.info("STOPPING current gProfiler instance...")
            try:
                self.current_gprofiler.stop()  # This sets the stop_event!
                logger.info("Successfully called gprofiler.stop()")
            except Exception as e:
                # TODO: This is a huge leak, report it  
                logger.error(f"Error stopping gProfiler: {e}")
            finally:
                self.current_gprofiler = None
        
        if self.current_thread and self.current_thread.is_alive():
            # No need to actively kill the thread, the self.current_gprofiler.stop() already handles it using events
            logger.info("Waiting for profiler thread to finish...")
            self.current_thread.join(timeout=10)
            self.current_thread = None
    
    def _start_new_profiler(self, profiling_command: Dict[str, Any], command_id: str):
        """Start a new profiler with the given configuration"""
        try:
            # Import here to avoid circular imports
            from gprofiler.main import DEFAULT_PROFILING_DURATION
            
            # Create modified args for the new profiler
            new_args = self._create_profiler_args(profiling_command)
            
            # Create new GProfiler instance
            self.current_gprofiler = self._create_gprofiler_instance(new_args)
            
            # Start profiler in a separate thread
            self.current_thread = threading.Thread(
                target=self._run_profiler,
                args=(
                    self.current_gprofiler,
                    new_args.continuous,
                    getattr(new_args, "duration", DEFAULT_PROFILING_DURATION),
                    command_id,
                ),
                daemon=True
            )
            self.current_thread.start()
            
            logger.info(f"Started new gProfiler instance with command ID: {command_id}")
            
        except Exception as e:
            logger.error(f"Failed to start new profiler: {e}", exc_info=True)
            # Report failure to the server
            self.heartbeat_client.send_command_completion(
                command_id=command_id,
                status="failed",
                execution_time=0,
                error_message=str(e),
                results_path=None
            )
    
    def _create_profiler_args(self, profiling_command: Dict[str, Any]) -> configargparse.Namespace:
        """Create modified args based on profiling command"""
        # Copy base args
        new_args = configargparse.Namespace(**vars(self.base_args))
        
        # Update with profiling command parameters from combined_config
        combined_config = profiling_command.get("combined_config", {})
        if "duration" in combined_config:
            new_args.duration = combined_config["duration"]
        if "frequency" in combined_config:
            new_args.frequency = combined_config["frequency"]
        if "profiling_mode" in combined_config:
            new_args.profiling_mode = combined_config["profiling_mode"]
        if "target_hostnames" in combined_config and combined_config["target_hostnames"]:
            # Only profile if this hostname is in the target list or no specific targets
            if self.heartbeat_client.hostname not in combined_config["target_hostnames"]:
                logger.info(f"Hostname {self.heartbeat_client.hostname} not in target list, skipping profiling")
                return None
        if "pids" in combined_config and combined_config["pids"]:
            new_args.pids_to_profile = combined_config["pids"]
        
        # Set continuous mode
        new_args.continuous = combined_config.get("continuous", False)
        
        return new_args
    
    def _create_gprofiler_instance(self, args: configargparse.Namespace) -> 'GProfiler':
        """Create a new GProfiler instance with the given args"""
        if args is None:
            return None
        
        # Import here to avoid circular imports
        from gprofiler.main import GProfiler, pids_to_processes
            
        processes_to_profile = pids_to_processes(args)
        state = get_state()
        
        # Create profiler API client
        profiler_api_client = None
        if args.upload_results:
            profiler_api_client = ProfilerAPIClient(
                token=args.server_token,
                service_name=args.service_name,
                server_address=args.server_host,
                curlify_requests=getattr(args, 'curlify_requests', False),
                hostname=get_hostname(),
                verify=args.verify,
                upload_timeout=getattr(args, 'server-upload-timeout', 120)  # Default to 120 seconds
            )
        
        enrichment_options = EnrichmentOptions(
            profile_api_version=args.profile_api_version,
            container_names=args.container_names,
            application_identifiers=args.collect_appids,
            application_identifier_args_filters=args.app_id_args_filters,
            application_metadata=args.application_metadata,
        )
        
        # Create external metadata path if specified
        external_metadata_path = None
        if hasattr(args, 'external_metadata') and args.external_metadata:
            external_metadata_path = Path(args.external_metadata)
        
        # Create heartbeat file path if specified
        heartbeat_file_path = None
        if hasattr(args, 'heartbeat_file') and args.heartbeat_file:
            heartbeat_file_path = Path(args.heartbeat_file)
        
        # Create perfspect path if specified
        perfspect_path = None
        if hasattr(args, "tool_perfspect_path") and args.tool_perfspect_path:
            perfspect_path = Path(args.tool_perfspect_path)
        
        return GProfiler(
            output_dir=getattr(args, 'output_dir', None),
            flamegraph=getattr(args, 'flamegraph', True),
            rotating_output=getattr(args, 'rotating_output', False),
            rootless=getattr(args, 'rootless', False),
            profiler_api_client=profiler_api_client,
            collect_metrics=getattr(args, 'collect_metrics', True),
            collect_metadata=getattr(args, 'collect_metadata', True),
            enrichment_options=enrichment_options,
            state=state,
            usage_logger=NoopUsageLogger(),  # Simplified for dynamic profiling
            user_args=args.__dict__,
            duration=args.duration,
            profile_api_version=args.profile_api_version,
            profiling_mode=args.profiling_mode,
            collect_hw_metrics=getattr(args, "collect_hw_metrics", False),
            profile_spawned_processes=getattr(args, 'profile_spawned_processes', False),
            remote_logs_handler=None,  # Simplified for dynamic profiling
            controller_process=None,
            processes_to_profile=processes_to_profile,
            external_metadata_path=external_metadata_path,
            heartbeat_file_path=heartbeat_file_path,
            perfspect_path=perfspect_path,
            perfspect_duration=getattr(args, "tool_perfspect_duration", 60),
        )
    
    def _run_profiler(self, gprofiler: 'GProfiler', continuous: bool, duration: int, command_id: str):
        """Run the profiler with specified args"""
        if gprofiler is None:
            return
            
        start_time = datetime.datetime.now()
        error_message = None
        results_path = None
        
        try:
            if continuous:
                logger.info(f"Running continuous profiler for command ID: {command_id}")
                gprofiler.run_continuous()
            else:
                logger.info(f"Running profiler for {duration} seconds (command ID: {command_id})...")
                gprofiler.run_single()

            # After run completes, check if it was stopped or completed
            if gprofiler._profiler_state.stop_event.is_set():
                logger.info(f"Profiler run was stopped before completion for command ID: {command_id}")
            else:
                logger.info(f"Profiler run completed successfully for command ID: {command_id}")
            
            # Try to get results path if available
            if hasattr(gprofiler, 'output_dir') and gprofiler.output_dir:
                results_path = str(gprofiler.output_dir)
                
        except Exception as e:
            # Internal exceptions can occur during profiling stop
            # Only consider a failure if it was not due to a stop event
            if not gprofiler._profiler_state.stop_event.is_set():
                error_message = str(e)
                logger.error(f"Profiler run failed for command ID {command_id}: {e}", exc_info=True)
            else:
                logger.info(f"Profiler run was stopped before completion for command ID: {command_id}")
            
        finally:
            # Calculate execution time
            end_time = datetime.datetime.now()
            execution_time = int((end_time - start_time).total_seconds())
            
            # Clear the current profiler reference
            if self.current_gprofiler == gprofiler:
                self.current_gprofiler = None
    
    def stop(self):
        """Stop the heartbeat manager"""
        logger.info("Stopping heartbeat manager...")
        self.stop_event.set()
        self._stop_current_profiler()