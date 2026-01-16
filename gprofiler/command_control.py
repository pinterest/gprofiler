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
import threading
from collections import deque
from dataclasses import dataclass
from typing import Dict, Any, Optional, Deque

logger = logging.getLogger(__name__)

# Command queue size limits
ADHOC_QUEUE_MAX_SIZE = 10  # Maximum ad-hoc commands to queue
CONTINUOUS_QUEUE_MAX_SIZE = 1  # Maximum continuous commands to queue


@dataclass
class ProfilingCommand:
    """Represents a profiling command with metadata"""
    command_id: str
    command_type: str  # 'start' or 'stop'
    profiling_command: Dict[str, Any]
    is_continuous: bool
    timestamp: datetime.datetime


class CommandManager:
    """Manager for profiling command queues with priority-based execution"""

    def __init__(self):
        # Command queues
        self.adhoc_queue: Deque[ProfilingCommand] = deque()  # For single-run commands (continuous=False)
        self.continuous_queue: Deque[ProfilingCommand] = deque()  # For continuous commands (continuous=True)
        self.queue_lock = threading.Lock()  # Thread-safe queue operations

    def enqueue_command(self, command: Dict[str, Any] | ProfilingCommand) -> ProfilingCommand:
        """Enqueue a command to the appropriate queue

        Args:
            command: Either a ProfilingCommand object or a command response dictionary

        Returns:
            ProfilingCommand object that was created and enqueued (or False if queue was full)
        """
        # Convert dict to ProfilingCommand if needed
        if not isinstance(command, ProfilingCommand):
            profiling_command = command["profiling_command"]
            command_id = command["command_id"]
            command_type = profiling_command.get("command_type", "start")

            # Extract continuous flag from combined_config
            combined_config = profiling_command.get("combined_config", {})
            is_continuous = combined_config.get("continuous", False)

            # Create ProfilingCommand object
            cmd = ProfilingCommand(
                command_id=command_id,
                command_type=command_type,
                profiling_command=profiling_command,
                is_continuous=is_continuous,
                timestamp=datetime.datetime.now()
            )
        else:
            cmd = command

        # Add to appropriate queue
        with self.queue_lock:
            if cmd.is_continuous:
                # Check if continuous queue is full
                if len(self.continuous_queue) >= CONTINUOUS_QUEUE_MAX_SIZE:
                    logger.warning(f"Continuous queue full (max: {CONTINUOUS_QUEUE_MAX_SIZE}), dropping incoming command {cmd.command_id}")
                    return False

                self.continuous_queue.append(cmd)
                logger.info(f"Enqueued continuous command {cmd.command_id} (queue size: {len(self.continuous_queue)})")
            else:
                # Check if ad-hoc queue is full
                if len(self.adhoc_queue) >= ADHOC_QUEUE_MAX_SIZE:
                    logger.warning(f"Ad-hoc queue full (max: {ADHOC_QUEUE_MAX_SIZE}), dropping incoming command {cmd.command_id}")
                    return False

                self.adhoc_queue.append(cmd)
                logger.info(f"Enqueued ad-hoc command {cmd.command_id} (queue size: {len(self.adhoc_queue)})")

        return cmd

    def get_next_command(self) -> Optional[ProfilingCommand]:
        """Fetch the next command to execute based on priority logic.

        Priority strategy:
        1. Ad-hoc commands have higher priority than continuous commands
        2. Within each queue, FIFO order is maintained
        3. If no ad-hoc commands exist, fetch from continuous queue

        Returns:
            ProfilingCommand if available, None otherwise
        """
        with self.queue_lock:
            # Priority 1: Ad-hoc commands (single-run, immediate execution)
            if self.adhoc_queue:
                cmd = self.adhoc_queue.popleft()
                logger.info(f"Fetched ad-hoc command {cmd.command_id} from queue (remaining: {len(self.adhoc_queue)})")
                return cmd

            # Priority 2: Continuous commands (long-running)
            if self.continuous_queue:
                cmd = self.continuous_queue.popleft()
                logger.info(f"Fetched continuous command {cmd.command_id} from queue (remaining: {len(self.continuous_queue)})")
                return cmd

            logger.debug("No commands in queues")
            return None

    def has_queued_commands(self) -> bool:
        """Check if there are any commands in the queues

        Returns:
            True if either queue has pending commands, False otherwise
        """
        with self.queue_lock:
            return len(self.adhoc_queue) > 0 or len(self.continuous_queue) > 0

    def clear_queues(self):
        """Clear all queued commands (used during shutdown)"""
        with self.queue_lock:
            adhoc_count = len(self.adhoc_queue)
            continuous_count = len(self.continuous_queue)
            self.adhoc_queue.clear()
            self.continuous_queue.clear()
            if adhoc_count > 0 or continuous_count > 0:
                logger.info(f"Cleared {adhoc_count} ad-hoc and {continuous_count} continuous commands from queues")
