"""App Server 模块测试"""
import pytest
from agent_manager.core.app_server.thread_utils import (
    _timestamp_iso,
    _codex_thread_list_params,
    _subagent_turn_status
)


def test_timestamp_iso_converts_valid_timestamps():
    """测试时间戳转换"""
    # Unix时间戳（秒）
    result = _timestamp_iso(1609459200)
    assert result is not None
    assert "2021" in result

    # Unix时间戳（毫秒）
    result = _timestamp_iso(1609459200000)
    assert result is not None
    assert "2021" in result


def test_timestamp_iso_handles_invalid_input():
    """测试无效输入处理"""
    assert _timestamp_iso(None) is None
    assert _timestamp_iso("invalid") is None
    assert _timestamp_iso([]) is None


def test_codex_thread_list_params():
    """测试线程列表参数构建"""
    params = _codex_thread_list_params(50, False, False, None)

    assert params["limit"] == 50
    assert params["archived"] is False
    assert params["sortKey"] == "updated_at"
    assert "sourceKinds" in params


def test_codex_thread_list_params_respects_limits():
    """测试参数限制"""
    # 超出最大限制
    params = _codex_thread_list_params(200, False, False)
    assert params["limit"] == 100

    # 低于最小限制
    params = _codex_thread_list_params(0, False, False)
    assert params["limit"] == 1


def test_subagent_turn_status_extracts_from_thread():
    """测试子代理回合状态提取"""
    thread = {
        "turns": [
            {"id": "turn1", "status": {"type": "completed"}},
            {"id": "turn2", "status": {"type": "in_progress"}}
        ]
    }

    status, turn_id = _subagent_turn_status(thread)
    assert status == "in_progress"
    assert turn_id == "turn2"


def test_subagent_turn_status_handles_empty_thread():
    """测试空线程处理"""
    status, turn_id = _subagent_turn_status({})
    assert status == ""
    assert turn_id is None
