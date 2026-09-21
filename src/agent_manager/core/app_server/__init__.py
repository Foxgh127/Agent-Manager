"""
App Server 模块 - 模块化版本

将原始的 app_server.py (581行) 拆分为：
- client.py: 客户端请求处理
- thread_utils.py: 线程相关工具函数
- health.py: 健康检查和清理
- management.py: 线程管理操作

向后兼容：所有原始函数都通过此 __init__.py 重新导出
"""

# 导出所有公共 API
from .client import (
    codex_app_server_requests,
    codex_app_server_request,
)

from .thread_utils import (
    _timestamp_iso,
    _codex_thread_list_params,
    _codex_thread_rows,
    _subagent_turn_status,
)

from .health import (
    stale_subagent_health,
    cleanup_stale_subagents,
)

from .management import (
    list_codex_thread_groups,
    refresh_codex_history_index,
    manage_codex_threads,
    rename_codex_thread,
)

__all__ = [
    # Client
    'codex_app_server_requests',
    'codex_app_server_request',

    # Thread Utils
    '_timestamp_iso',
    '_codex_thread_list_params',
    '_codex_thread_rows',
    '_subagent_turn_status',

    # Health
    'stale_subagent_health',
    'cleanup_stale_subagents',

    # Management
    'list_codex_thread_groups',
    'refresh_codex_history_index',
    'manage_codex_threads',
    'rename_codex_thread',
]
