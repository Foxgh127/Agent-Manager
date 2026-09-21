from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from agent_manager.core import process_utils
from agent_manager.core.process_utils import (
    ProcessCheckMode,
    ProcessInfo,
    detect_codex_processes,
    kill_process_tree,
    validate_pid,
    wait_for_process_exit,
)


def test_parse_fast_processes_validates_pid_and_name() -> None:
    output = (
        '[{"Id": 1234, "ProcessName": "codex", "Path": "C:\\\\codex.exe"},'
        ' {"Id": 0, "ProcessName": "codex", "Path": "C:\\\\bad.exe"},'
        ' {"Id": 9876, "ProcessName": "notepad", "Path": "C:\\\\notepad.exe"}]'
    )

    processes = process_utils._parse_windows_processes(output, ProcessCheckMode.FAST)

    assert processes == [
        ProcessInfo(1234, "codex", "C:\\codex.exe", "", None),
    ]


def test_detect_processes_retries_empty_result() -> None:
    with patch.object(process_utils, "_detect_processes_internal", side_effect=[[], [
        ProcessInfo(1234, "codex", "C:\\codex.exe", "", None),
    ]]) as detect, patch.object(process_utils.time, "sleep") as sleep:
        result = detect_codex_processes(
            mode=ProcessCheckMode.THOROUGH,
            retry_count=1,
            retry_delay=0.25,
        )

    assert result[0].pid == 1234
    assert detect.call_count == 2
    sleep.assert_called_once_with(0.25)


def test_invalid_pids_are_rejected_before_process_operations() -> None:
    for invalid in (0, -1, True, "1234", 1 << 63):
        with pytest.raises(ValueError):
            validate_pid(invalid)

    with pytest.raises(ValueError):
        kill_process_tree("1234")  # type: ignore[arg-type]


def test_kill_does_not_terminate_current_process() -> None:
    assert kill_process_tree(process_utils.os.getpid()) is False


def test_wait_for_process_exit_uses_validated_pid() -> None:
    with patch.object(process_utils, "_pid_exists", side_effect=[True, False]), patch.object(
        process_utils.time, "sleep"
    ) as sleep:
        assert wait_for_process_exit(4321, timeout=1) is True
    sleep.assert_called_once()
