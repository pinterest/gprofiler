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

import re
from collections import Counter, defaultdict
from enum import Enum
from pathlib import Path
from threading import Event
from typing import List, Optional

from psutil import Process

from gprofiler.exceptions import CalledProcessError, PerfNoSupportedEvent
from gprofiler.gprofiler_types import ProcessToStackSampleCounters
from gprofiler.log import get_logger_adapter
from gprofiler.utils import run_process
from gprofiler.utils.perf_process import PerfProcess, perf_path, _is_pid_related_error

logger = get_logger_adapter(__name__)

# ffffffff81082227 mmput+0x57 ([kernel.kallsyms])
# 0 [unknown] ([unknown])
# 7fe48f00faff __poll+0x4f (/lib/x86_64-linux-gnu/libc-2.31.so)
FRAME_REGEX = re.compile(
    r"""
    ^\s*[0-9a-f]+[ ]                                 # first a hexadecimal offset
    (?P<symbol>.*)[ ]                                # a symbol name followed by a space
    \( (?:                                           # dso name is either:
        \[ (?P<dso_brackets> [^]]+) \]               # - text enclosed in square brackets, e.g.: [vdso]
        | (?P<dso_plain> [^)]+(?:[ ]\(deleted\))? )  # - OR library name, optionally followed by " (deleted)" tag
    ) \)$""",
    re.VERBOSE,
)
SAMPLE_REGEX = re.compile(
    r"\s*(?P<comm>.+?)\s+(?P<pid>[\d-]+)/(?P<tid>[\d-]+)(?:\s+\[(?P<cpu>\d+)])?\s+(?P<time>\d+\.\d+):\s+"
    r"(?:(?P<freq>\d+)\s+)?(?P<event_family>[\w\-_/]+):(?:(?P<event>[\w-]+):)?(?P<suffix>[^\n]*)(?:\n(?P<stack>.*))?",
    re.MULTILINE | re.DOTALL,
)


class SupportedPerfEvent(Enum):
    """
    order here is crucial, the first one we try and succeed - will be used.
    keep it mind that we should always use `PERF_DEFAULT` as a first try.
    meaning - keeping with `perf`s' default is always preferred.
    """

    PERF_DEFAULT = "default"
    PERF_SW_CPU_CLOCK = "cpu-clock"
    PERF_SW_TASK_CLOCK = "task-clock"

    def perf_extra_args(self) -> List[str]:
        if self == SupportedPerfEvent.PERF_DEFAULT:
            return []
        return ["-e", self.value]


def discover_appropriate_perf_event(
    tmp_dir: Path, stop_event: Event, pids: Optional[List[Process]] = None
) -> SupportedPerfEvent:
    """
    Get the appropriate event should be used by `perf record`.

    We've observed that on some machines the default event `perf record` chooses doesn't actually collect samples.
    And we generally would not want to change the default event chosen by `perf record`, so before
    any change we apply to collected sample event, we want to make sure that the default event
    actually collects samples, and make changes only if it doesn't.

    :param tmp_dir: working directory of this function
    :return: `perf record` extra arguments to use (e.g. `["-e", "cpu-clock"]`)
    """

    segfault_count = 0
    pid_failure_count = 0
    total_events = len(SupportedPerfEvent)
    
    for event in SupportedPerfEvent:
        perf_script_output = None

        try:
            current_extra_args = event.perf_extra_args() + [
                "--",
                "sleep",
                "0.5",
            ]  # `sleep 0.5` is enough to be certain some samples should've been collected.
            perf_process = PerfProcess(
                frequency=11,
                stop_event=stop_event,
                output_path=str(tmp_dir / "perf_default_event.fp"),
                is_dwarf=False,
                inject_jit=False,
                extra_args=current_extra_args,
                processes_to_profile=pids,
                switch_timeout_s=15,
            )
            perf_process.start()
            parsed_perf_script = parse_perf_script(perf_process.wait_and_script())
            if len(parsed_perf_script) > 0:
                # `perf script` isn't empty, we'll use this event.
                return event
        except Exception as e:  # pylint: disable=broad-except
            # Check if this was a segfault in perf script, log it appropriately
            exc_name = type(e).__name__
            error_message = str(e)
            
            # Check if this looks like a segfault-related error 
            # Handle both CalledProcessError with negative return codes and other segfault-related exceptions
            is_segfault = False
            if "CalledProcessError" in exc_name and hasattr(e, 'returncode') and getattr(e, 'returncode', 0) < 0:
                is_segfault = True
                signal_num = -getattr(e, 'returncode', 0)
                logger.warning(
                    f"Perf event {event.name} failed with signal {signal_num}, "
                    f"likely segfault. This is known to happen on some GPU machines.",
                    perf_event=event.name,
                )
            # Also check for segfault-related error messages in the exception text
            elif any(keyword in error_message.lower() for keyword in ['sigsegv', 'segfault', 'segmentation fault', 'signal 11']):
                is_segfault = True
                logger.warning(
                    f"Perf event {event.name} failed with segfault-related error. "
                    f"This is known to happen on some GPU machines. Error: {error_message}",
                    perf_event=event.name,
                )
            
            if is_segfault:
                segfault_count += 1
            # Check if this is a PID-related failure  
            elif pids is not None and _is_pid_related_error(error_message):
                pid_failure_count += 1
                logger.warning(
                    f"Perf event {event.name} failed due to target process issues. "
                    f"One or more target processes may have exited during discovery. "
                    f"Error: {error_message}",
                    perf_event=event.name,
                )
            else:
                logger.warning(
                    f"Failed to collect samples for perf event ({exc_name})",
                    exc_info=True,
                    perf_event=event.name,
                    perf_script_output=perf_script_output,
                )
        finally:
            perf_process.stop()

    # If all events failed due to segfaults, provide a specific error message
    if segfault_count == total_events:
        logger.critical(
            f"All perf events failed with segfaults ({segfault_count}/{total_events}). "
            f"This is a known issue on some GPU machines. "
            f"Consider running with '--perf-mode disabled' to avoid using perf."
        )
        # For GPU machines where all events segfault, return a default event anyway
        # The runtime perf script handling will gracefully handle the segfaults
        logger.info(
            f"GPU machine detected - using default perf event despite segfaults. "
            f"Runtime perf script crashes will be handled gracefully."
        )
        return SupportedPerfEvent.PERF_DEFAULT
    # If all events failed due to PID issues, provide a specific error message
    elif pid_failure_count == total_events:
        logger.critical(
            f"All perf events failed due to target process issues ({pid_failure_count}/{total_events}). "
            f"Target processes may have exited during discovery. "
            f"Consider using system-wide profiling or '--perf-mode disabled' to avoid using perf."
        )
    
    raise PerfNoSupportedEvent


def can_i_use_perf_events() -> bool:
    # checks access to perf_events
    # TODO invoking perf has a toll of about 1 second on my box; maybe we want to directly call
    # perf_event_open here for this test?
    try:
        run_process([perf_path(), "record", "-o", "/dev/null", "--", "/bin/true"])
    except CalledProcessError as e:
        assert isinstance(e.stderr, str), f"unexpected type {type(e.stderr)}"

        # perf's output upon start error (e.g due to permissions denied error)
        if not (
            e.returncode == 255
            and (
                "Access to performance monitoring and observability operations is limited" in e.stderr
                or "perf_event_open(..., PERF_FLAG_FD_CLOEXEC) failed with unexpected error" in e.stderr
                or "Permission error mapping pages.\n" in e.stderr
            )
        ):
            logger.warning(
                "Unexpected perf exit code / error output, returning False for perf check anyway", exc_info=True
            )
        return False
    else:
        # all good
        return True


def valid_perf_pid(pid: int) -> bool:
    """
    perf, in some cases, reports PID 0 / -1. These are not real PIDs and we don't want to
    try and look up the processes related to them.
    """
    return pid not in (0, -1)


def collapse_stack(comm: str, stack: str, insert_dso_name: bool = False) -> str:
    """
    Collapse a single stack from "perf".
    """
    funcs = [comm]
    for line in reversed(stack.splitlines()):
        m = FRAME_REGEX.match(line)
        assert m is not None, f"bad line: {line}"
        sym, dso = m.group("symbol"), m.group("dso_brackets") or m.group("dso_plain")
        sym = sym.split("+")[0]  # strip the offset part.
        if sym == "[unknown]" and dso != "unknown":
            sym = f"({dso})"
        # append kernel annotation
        elif "kernel" in dso or "vmlinux" in dso:
            sym += "_[k]"
        elif insert_dso_name:
            sym += f" ({dso})"
        funcs.append(sym)
    return ";".join(funcs)


def parse_perf_script(script: Optional[str], insert_dso_name: bool = False) -> ProcessToStackSampleCounters:
    pid_to_collapsed_stacks_counters: ProcessToStackSampleCounters = defaultdict(Counter)

    if script is None:
        return pid_to_collapsed_stacks_counters

    for sample in script.split("\n\n"):
        try:
            if sample.strip() == "":
                continue
            if sample.startswith("#"):
                continue
            match = SAMPLE_REGEX.match(sample)
            if match is None:
                raise Exception("Failed to match sample")
            sample_dict = match.groupdict()

            pid = int(sample_dict["pid"])
            comm = sample_dict["comm"]
            stack = sample_dict["stack"]
            if stack is not None:
                pid_to_collapsed_stacks_counters[pid][collapse_stack(comm, stack, insert_dso_name)] += 1
        except Exception:
            logger.exception(f"Error processing sample: {sample}")
    return pid_to_collapsed_stacks_counters
