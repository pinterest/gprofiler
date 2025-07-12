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
import os
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
from gprofiler.state import State, init_state
from gprofiler.system_metrics import NoopSystemMetricsMonitor, SystemMetricsMonitor, SystemMetricsMonitorBase
from gprofiler.usage_loggers import NoopUsageLogger
from gprofiler.utils import TEMPORARY_STORAGE_PATH
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
        self.executed_command_ids: set = set()  # Track executed command IDs for idempotency
        self.command_ids_file = "/tmp/gprofiler_executed_commands.txt"  # Persist across restarts
        self.max_command_history = 1000  # Limit command history to prevent memory growth
        self.session = requests.Session()
        
        # Load previously executed command IDs
        self._load_executed_command_ids()
        
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
    
    def send_heartbeat(self) -> Optional[Dict[str, Any]]:
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

    def _load_executed_command_ids(self):
        """Load previously executed command IDs from file for persistence across restarts"""
        try:
            if os.path.exists(self.command_ids_file):
                with open(self.command_ids_file, 'r') as f:
                    command_ids = f.read().strip().split('\n')
                    self.executed_command_ids = set(filter(None, command_ids))  # Remove empty strings
                    logger.info(f"Loaded {len(self.executed_command_ids)} previously executed command IDs")
        except Exception as e:
            logger.warning(f"Failed to load executed command IDs: {e}")
            self.executed_command_ids = set()
    
    def _save_executed_command_id(self, command_id: str):
        """Save executed command ID to file for persistence"""
        try:
            with open(self.command_ids_file, 'a') as f:
                f.write(f"{command_id}\n")
        except Exception as e:
            logger.warning(f"Failed to save executed command ID {command_id}: {e}")
    
    def mark_command_executed(self, command_id: str):
        """Mark a command as executed and persist it"""
        self.executed_command_ids.add(command_id)
        self._save_executed_command_id(command_id)
        
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
                
                # Rewrite the file with only the commands we're keeping
                try:
                    with open(self.command_ids_file, 'w') as f:
                        for cmd_id in self.executed_command_ids:
                            f.write(f"{cmd_id}\n")
                    logger.info(f"Cleaned up command ID history, keeping {len(self.executed_command_ids)} entries")
                except Exception as e:
                    logger.warning(f"Failed to rewrite command IDs file: {e}")
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
    
    def start_heartbeat_loop(self):
        """Start the main heartbeat loop"""
        logger.info("Starting heartbeat loop...")
        
        while not self.stop_event.is_set():
            try:
                # Send heartbeat and check for commands
                command_response = self.heartbeat_client.send_heartbeat()
                
                if command_response and command_response.get("profiling_command"):
                    profiling_command = command_response["profiling_command"]
                    command_id = command_response["command_id"]
                    command_type = profiling_command.get("command_type", "start")
                    
                    # Check for idempotency - skip if command already executed
                    if command_id in self.heartbeat_client.executed_command_ids:
                        logger.info(f"Command ID {command_id} already executed, skipping...")
                        continue
                    
                    logger.info(f"Received {command_type} command: {command_id}")
                    
                    # Mark command as executed for idempotency
                    self.heartbeat_client.mark_command_executed(command_id)
                    self.heartbeat_client.last_command_id = command_id
                    
                    if command_type == "stop":
                        # Stop current profiler without starting a new one
                        logger.info("Stopping profiler due to stop command")
                        self._stop_current_profiler()
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
                        self._stop_current_profiler()
                        self._start_new_profiler(profiling_command, command_id)
                        # Note: command completion will be reported by _run_profiler when profiling finishes
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
            logger.info("Stopping current gProfiler instance...")
            try:
                self.current_gprofiler.stop()
            except Exception as e:
                logger.error(f"Error stopping gProfiler: {e}")
            finally:
                self.current_gprofiler = None
        
        if self.current_thread and self.current_thread.is_alive():
            logger.info("Waiting for profiler thread to finish...")
            self.current_thread.join(timeout=10)
            self.current_thread = None
    
    def _start_new_profiler(self, profiling_command: Dict[str, Any], command_id: str):
        """Start a new profiler with the given configuration"""
        try:
            # Create modified args for the new profiler
            new_args = self._create_profiler_args(profiling_command)
            
            # Create new GProfiler instance
            self.current_gprofiler = self._create_gprofiler_instance(new_args)
            
            # Start profiler in a separate thread
            self.current_thread = threading.Thread(
                target=self._run_profiler,
                args=(self.current_gprofiler, profiling_command.get("duration", 60), command_id),
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
        
        # Set continuous mode to False for on-demand profiling
        new_args.continuous = False
        
        return new_args
    
    def _create_gprofiler_instance(self, args: configargparse.Namespace) -> 'GProfiler':
        """Create a new GProfiler instance with the given args"""
        if args is None:
            return None
        
        # Import here to avoid circular imports
        from gprofiler.main import GProfiler, pids_to_processes
            
        processes_to_profile = pids_to_processes(args)
        state = init_state()
        
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
    
    def _run_profiler(self, gprofiler: 'GProfiler', duration: int, command_id: str):
        """Run the profiler for the specified duration"""
        if gprofiler is None:
            return
            
        start_time = datetime.datetime.now()
        status = "failed"
        error_message = None
        results_path = None
        
        try:
            logger.info(f"Running profiler for {duration} seconds (command ID: {command_id})...")
            gprofiler.run_single()
            status = "completed"
            logger.info(f"Profiler run completed successfully for command ID: {command_id}")
            
            # Try to get results path if available
            if hasattr(gprofiler, 'output_dir') and gprofiler.output_dir:
                results_path = str(gprofiler.output_dir)
                
        except Exception as e:
            status = "failed"
            error_message = str(e)
            logger.error(f"Profiler run failed for command ID {command_id}: {e}", exc_info=True)
            
        finally:
            # Calculate execution time
            end_time = datetime.datetime.now()
            execution_time = int((end_time - start_time).total_seconds())
            
            # Report command completion to the server
            try:
                self.heartbeat_client.send_command_completion(
                    command_id=command_id,
                    status=status,
                    execution_time=execution_time,
                    error_message=error_message,
                    results_path=results_path
                )
            except Exception as e:
                logger.error(f"Failed to report command completion for {command_id}: {e}")
            
            # Clear the current profiler reference
            if self.current_gprofiler == gprofiler:
                self.current_gprofiler = None
    
    def stop(self):
        """Stop the heartbeat manager"""
        logger.info("Stopping heartbeat manager...")
        self.stop_event.set()
        self._stop_current_profiler()