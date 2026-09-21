# 修复清单 091-120: switching.py 和 app_server.py 拆分完成

## 修复 091-095: 完成 switching.py 拆分

### 091: 创建 switching 包结构

**新建**: `src/agent_manager/core/switching/__init__.py`

```python
"""账号切换模块 - 统一入口"""

from .progress_reporter import SwitchProgressReporter, SwitchStage
from .transaction_manager import TransactionManager, SwitchTransaction
from .validators import (
    validate_account_config,
    validate_provider_config,
    validate_switch_prerequisites
)
from .launch_verifier import LaunchVerifier
from .account_switcher import AccountSwitcher
from .launch_coordinator import LaunchCoordinator

__all__ = [
    'SwitchProgressReporter',
    'SwitchStage',
    'TransactionManager',
    'SwitchTransaction',
    'validate_account_config',
    'validate_provider_config',
    'validate_switch_prerequisites',
    'LaunchVerifier',
    'AccountSwitcher',
    'LaunchCoordinator',
]
```

---

### 092: 创建账号切换器

**新建**: `src/agent_manager/core/switching/account_switcher.py`

```python
"""账号切换核心逻辑"""
from pathlib import Path
from typing import Optional, Tuple
from agent_manager.core.switching.progress_reporter import SwitchProgressReporter
from agent_manager.core.switching.transaction_manager import TransactionManager
from agent_manager.core.switching.validators import validate_switch_prerequisites
from agent_manager.core.process_utils import detect_codex_processes, kill_process_tree
from agent_manager.storage.snapshot_service import ConfigSnapshotProvider
from agent_manager.core.configuration import apply_configuration


class AccountSwitcher:
    """账号切换器"""
    
    def __init__(
        self,
        config_path: Path,
        snapshot_dir: Path,
        reporter: Optional[SwitchProgressReporter] = None
    ):
        self.config_path = config_path
        self.reporter = reporter or SwitchProgressReporter()
        self.transaction_manager = TransactionManager(snapshot_dir)
    
    def switch_account(
        self,
        source_account: Optional[dict],
        target_account: dict
    ) -> Tuple[bool, str]:
        """
        切换账号
        
        Returns:
            (success, message)
        """
        # 验证前提条件
        self.reporter.validating()
        
        codex_running = len(detect_codex_processes()) > 0
        is_valid, error = validate_switch_prerequisites(
            source_account,
            target_account,
            codex_running
        )
        
        if not is_valid:
            self.reporter.failed(f"验证失败: {error}")
            return False, error
        
        # 创建事务
        import uuid
        transaction_id = f"switch_{uuid.uuid4().hex[:8]}"
        transaction = self.transaction_manager.begin_transaction(transaction_id)
        
        try:
            # 停止进程
            if codex_running:
                self.reporter.stopping_processes()
                if not self._stop_codex_processes():
                    raise Exception("Failed to stop Codex processes")
            
            # 备份配置
            self.reporter.backing_up()
            config_provider = ConfigSnapshotProvider(self.config_path)
            transaction.save_snapshot("config", config_provider)
            
            # 应用新配置
            self.reporter.applying_config()
            config_updates = self._build_config_updates(target_account)
            apply_configuration(self.config_path, config_updates)
            
            # 提交事务
            transaction.commit()
            self.reporter.completed("账号切换成功")
            
            return True, "Switch completed"
        
        except Exception as e:
            # 回滚
            self.reporter.failed(f"切换失败: {str(e)}")
            try:
                transaction.rollback("config", config_provider)
            except:
                pass
            
            return False, str(e)
        
        finally:
            self.transaction_manager.end_transaction(transaction_id)
    
    def _stop_codex_processes(self) -> bool:
        """停止所有 Codex 进程"""
        processes = detect_codex_processes()
        
        for proc in processes:
            if not kill_process_tree(proc.pid):
                return False
        
        # 验证所有进程已停止
        import time
        time.sleep(1)
        remaining = detect_codex_processes()
        
        return len(remaining) == 0
    
    def _build_config_updates(self, account: dict) -> dict:
        """构建配置更新"""
        updates = {}
        
        # API 相关
        if 'api_key' in account:
            updates['api.key'] = account['api_key']
        
        if 'api_endpoint' in account:
            updates['api.endpoint'] = account['api_endpoint']
        
        # 账号信息
        if 'email' in account:
            updates['user.email'] = account['email']
        
        if 'provider' in account:
            updates['provider.name'] = account['provider']
        
        return updates
```

---

### 093: 创建启动协调器

**新建**: `src/agent_manager/core/switching/launch_coordinator.py`

```python
"""Codex 启动协调"""
from pathlib import Path
from typing import Optional, Tuple, List
import subprocess
from agent_manager.core.switching.launch_verifier import LaunchVerifier
from agent_manager.core.runtime.discovery import find_codex_executable


class LaunchCoordinator:
    """启动协调器"""
    
    def __init__(self, verifier: Optional[LaunchVerifier] = None):
        self.verifier = verifier or LaunchVerifier()
    
    def launch_codex(
        self,
        codex_path: Optional[Path] = None,
        args: Optional[List[str]] = None,
        wait_for_start: bool = True
    ) -> Tuple[bool, str]:
        """
        启动 Codex
        
        Returns:
            (success, message)
        """
        # 查找可执行文件
        if not codex_path:
            codex_path = find_codex_executable()
        
        if not codex_path:
            return False, "Codex executable not found"
        
        if not codex_path.exists():
            return False, f"Codex not found at {codex_path}"
        
        # 构建命令
        cmd = [str(codex_path)]
        if args:
            cmd.extend(args)
        
        # 启动进程
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        except Exception as e:
            return False, f"Failed to launch: {str(e)}"
        
        # 等待启动
        if wait_for_start:
            success, message = self.verifier.wait_for_process_start(codex_path)
            return success, message
        
        return True, "Codex launch initiated"
    
    def launch_with_config(
        self,
        config_path: Path,
        codex_path: Optional[Path] = None
    ) -> Tuple[bool, str]:
        """使用特定配置启动"""
        args = ['--config', str(config_path)]
        return self.launch_codex(codex_path, args)
    
    def verify_launch(self, expected_config: dict) -> Tuple[bool, str]:
        """验证启动结果"""
        # 验证进程
        from agent_manager.core.process_utils import detect_codex_processes
        processes = detect_codex_processes()
        
        if not processes:
            return False, "Codex process not found after launch"
        
        # 可以添加更多验证...
        
        return True, "Launch verified"
```

---

### 094: 更新 core/switching.py 使用新模块

**文件**: `src/agent_manager/core/switching.py`

简化为向后兼容入口：

```python
"""
Switching 模块 - 向后兼容入口

新代码应该使用 agent_manager.core.switching 包。
"""

from agent_manager.core.switching import *

import warnings
warnings.warn(
    "Direct import from switching.py is deprecated. "
    "Use agent_manager.core.switching package instead.",
    DeprecationWarning,
    stacklevel=2
)

# 保留任何不容易迁移的旧函数
# 使用新的 AccountSwitcher 实现
def switch_codex_account_and_launch(source, target, config_path):
    """向后兼容的切换函数"""
    from pathlib import Path
    from agent_manager.paths import get_data_dir
    
    switcher = AccountSwitcher(
        config_path=Path(config_path),
        snapshot_dir=get_data_dir() / "switch_snapshots"
    )
    
    success, message = switcher.switch_account(source, target)
    
    if success:
        from agent_manager.core.switching.launch_coordinator import LaunchCoordinator
        coordinator = LaunchCoordinator()
        return coordinator.launch_codex()
    
    return False, message
```

---

### 095: Switching 模块测试

**新建**: `tests/switching/test_account_switcher.py`

```python
import pytest
from pathlib import Path
from agent_manager.core.switching.account_switcher import AccountSwitcher


@pytest.fixture
def temp_config(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[api]\nkey = 'old_key'")
    return config_file


@pytest.fixture
def switcher(temp_config, tmp_path):
    return AccountSwitcher(
        config_path=temp_config,
        snapshot_dir=tmp_path / "snapshots"
    )


def test_validate_accounts(switcher):
    """测试账号验证"""
    source = {'id': '1', 'email': 'old@example.com', 'provider': 'openai'}
    target = {'id': '2', 'email': 'new@example.com', 'provider': 'anthropic'}
    
    # 应该能够验证有效账号
    from agent_manager.core.switching.validators import validate_account_config
    is_valid, _ = validate_account_config(target)
    assert is_valid


def test_build_config_updates(switcher):
    """测试配置更新构建"""
    account = {
        'api_key': 'new_key',
        'email': 'test@example.com',
        'provider': 'anthropic'
    }
    
    updates = switcher._build_config_updates(account)
    assert updates['api.key'] == 'new_key'
    assert updates['user.email'] == 'test@example.com'
```

---

## 修复 096-100: 完成 app_server.py 拆分

### 096: 创建 app_server 包结构

**新建**: `src/agent_manager/core/app_server/__init__.py`

```python
"""App Server 模块 - Codex App Server 通信"""

from .jsonrpc_client import JSONRPCClient, JSONRPCRequest, JSONRPCResponse
from .thread_pool import ManagedThreadPool, RequestQueue
from .session_manager import SessionManager, SessionInfo
from .codex_client import CodexAppServerClient

__all__ = [
    'JSONRPCClient',
    'JSONRPCRequest',
    'JSONRPCResponse',
    'ManagedThreadPool',
    'RequestQueue',
    'SessionManager',
    'SessionInfo',
    'CodexAppServerClient',
]
```

---

### 097: 创建会话管理器

**新建**: `src/agent_manager/core/app_server/session_manager.py`

```python
"""Codex 会话管理"""
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime


@dataclass
class SessionInfo:
    """会话信息"""
    session_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    model: str
    metadata: dict


class SessionManager:
    """会话管理器"""
    
    def __init__(self):
        self.sessions: dict[str, SessionInfo] = {}
    
    def add_session(self, session: SessionInfo):
        """添加会话"""
        self.sessions[session.session_id] = session
    
    def get_session(self, session_id: str) -> Optional[SessionInfo]:
        """获取会话"""
        return self.sessions.get(session_id)
    
    def list_sessions(
        self,
        limit: Optional[int] = None,
        offset: int = 0
    ) -> List[SessionInfo]:
        """列出会话"""
        all_sessions = sorted(
            self.sessions.values(),
            key=lambda s: s.updated_at,
            reverse=True
        )
        
        if limit:
            return all_sessions[offset:offset + limit]
        
        return all_sessions[offset:]
    
    def update_session(self, session_id: str, **updates):
        """更新会话信息"""
        if session_id in self.sessions:
            session = self.sessions[session_id]
            for key, value in updates.items():
                if hasattr(session, key):
                    setattr(session, key, value)
    
    def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        if session_id in self.sessions:
            del self.sessions[session_id]
            return True
        return False
    
    def search_sessions(self, query: str) -> List[SessionInfo]:
        """搜索会话"""
        results = []
        query_lower = query.lower()
        
        for session in self.sessions.values():
            if (query_lower in session.title.lower() or
                query_lower in session.session_id.lower()):
                results.append(session)
        
        return sorted(results, key=lambda s: s.updated_at, reverse=True)
```

---

### 098: 创建 Codex 客户端

**新建**: `src/agent_manager/core/app_server/codex_client.py`

```python
"""Codex App Server 客户端"""
import json
import socket
import threading
from typing import Optional, Callable, Any
from agent_manager.core.app_server.jsonrpc_client import (
    JSONRPCClient,
    JSONRPCRequest,
    JSONRPCResponse
)


class CodexAppServerClient(JSONRPCClient):
    """Codex App Server 客户端"""
    
    def __init__(self, host: str = "localhost", port: int = 9120):
        super().__init__()
        self.host = host
        self.port = port
        self.socket: Optional[socket.socket] = None
        self._lock = threading.Lock()
    
    def connect(self) -> bool:
        """连接到 App Server"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.host, self.port))
            return True
        except Exception:
            self.socket = None
            return False
    
    def disconnect(self):
        """断开连接"""
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
    
    def call(self, method: str, params: Any = None, timeout: float = 30.0) -> Any:
        """同步调用"""
        if not self.socket:
            raise ConnectionError("Not connected to App Server")
        
        with self._lock:
            # 创建请求
            request = self.create_request(method, params)
            request_json = request.to_json()
            
            # 发送请求
            self.socket.sendall(request_json.encode('utf-8') + b'\n')
            
            # 设置超时
            self.socket.settimeout(timeout)
            
            # 接收响应
            buffer = b''
            while b'\n' not in buffer:
                chunk = self.socket.recv(4096)
                if not chunk:
                    raise ConnectionError("Connection closed")
                buffer += chunk
            
            # 解析响应
            response_text = buffer.split(b'\n')[0].decode('utf-8')
            response = self.parse_response(response_text)
            
            if response.is_error:
                raise Exception(f"RPC Error: {response.error_message}")
            
            return response.result
    
    def list_sessions(self, limit: int = 50) -> list:
        """列出会话"""
        return self.call("listSessions", {"limit": limit})
    
    def get_session(self, session_id: str) -> dict:
        """获取会话详情"""
        return self.call("getSession", {"sessionId": session_id})
    
    def send_message(self, session_id: str, message: str) -> dict:
        """发送消息"""
        return self.call("sendMessage", {
            "sessionId": session_id,
            "message": message
        })
    
    def create_session(self, title: str, model: str) -> dict:
        """创建会话"""
        return self.call("createSession", {
            "title": title,
            "model": model
        })
```

---

### 099: 更新 core/app_server.py

**文件**: `src/agent_manager/core/app_server.py`

简化为向后兼容入口：

```python
"""
App Server 模块 - 向后兼容入口
"""

from agent_manager.core.app_server import *
from agent_manager.core.i18n import t

import warnings
warnings.warn(
    "Direct import from app_server.py is deprecated. "
    "Use agent_manager.core.app_server package instead.",
    DeprecationWarning,
    stacklevel=2
)

# 向后兼容的批量请求函数
def codex_app_server_requests(requests, timeout=30):
    """批量发送请求到 Codex App Server"""
    if not isinstance(requests, list):
        raise TypeError("Requests must be a list")
    
    if not 1 <= len(requests) <= 200:
        raise ValueError(t("errors.batch_request_range", min=1, max=200))
    
    client = CodexAppServerClient()
    
    if not client.connect():
        raise ConnectionError("Failed to connect to Codex App Server")
    
    try:
        results = []
        for req in requests:
            method = req.get('method')
            params = req.get('params')
            result = client.call(method, params, timeout)
            results.append(result)
        
        return results
    
    finally:
        client.disconnect()
```

---

### 100: App Server 模块测试

**新建**: `tests/app_server/__init__.py`

**新建**: `tests/app_server/test_session_manager.py`

```python
import pytest
from datetime import datetime
from agent_manager.core.app_server.session_manager import SessionManager, SessionInfo


@pytest.fixture
def manager():
    return SessionManager()


@pytest.fixture
def sample_session():
    return SessionInfo(
        session_id="sess_123",
        title="Test Session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        message_count=5,
        model="gpt-4",
        metadata={}
    )


def test_add_and_get_session(manager, sample_session):
    """测试添加和获取会话"""
    manager.add_session(sample_session)
    
    retrieved = manager.get_session("sess_123")
    assert retrieved is not None
    assert retrieved.title == "Test Session"


def test_list_sessions(manager, sample_session):
    """测试列出会话"""
    manager.add_session(sample_session)
    
    sessions = manager.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].session_id == "sess_123"


def test_delete_session(manager, sample_session):
    """测试删除会话"""
    manager.add_session(sample_session)
    
    success = manager.delete_session("sess_123")
    assert success is True
    
    retrieved = manager.get_session("sess_123")
    assert retrieved is None


def test_search_sessions(manager):
    """测试搜索会话"""
    session1 = SessionInfo(
        "sess_1", "Python Tutorial", datetime.now(),
        datetime.now(), 10, "gpt-4", {}
    )
    session2 = SessionInfo(
        "sess_2", "JavaScript Guide", datetime.now(),
        datetime.now(), 5, "gpt-4", {}
    )
    
    manager.add_session(session1)
    manager.add_session(session2)
    
    results = manager.search_sessions("Python")
    assert len(results) == 1
    assert results[0].title == "Python Tutorial"
```

---

## 修复 101-110: Gateway 重构使用中间件

### 101: 重构 Gateway Service

**文件**: `src/agent_manager/gateway/service.py`

在文件开头添加中间件导入和初始化：

```python
from agent_manager.gateway.middleware import MiddlewareChain
from agent_manager.gateway.middleware.auth import AuthMiddleware
from agent_manager.gateway.middleware.validation import RequestValidationMiddleware
from agent_manager.gateway.middleware.logging import LoggingMiddleware
from agent_manager.gateway.middleware.rate_limit import RateLimitMiddleware


class GatewayService:
    """Gateway 服务（重构版）"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.middleware_chain = MiddlewareChain()
        self._setup_middleware()
    
    def _setup_middleware(self):
        """设置中间件链"""
        # 按顺序添加中间件
        self.middleware_chain.use(LoggingMiddleware())
        self.middleware_chain.use(RateLimitMiddleware(max_requests=100, window_seconds=60))
        self.middleware_chain.use(AuthMiddleware(self.api_key))
        self.middleware_chain.use(RequestValidationMiddleware())
    
    async def handle_request(self, request: dict) -> dict:
        """处理请求"""
        # 构建处理器链
        handler = self.middleware_chain.build(self._final_handler)
        
        # 执行请求
        return await handler(request)
    
    async def _final_handler(self, request: dict) -> dict:
        """最终处理器 - 实际业务逻辑"""
        method = request.get('method', 'GET')
        path = request.get('path', '/')
        
        # 路由到具体处理器
        if path.startswith('/v1/chat/completions'):
            return await self._handle_chat_completion(request)
        elif path.startswith('/v1/models'):
            return await self._handle_models(request)
        else:
            return {
                'status': 404,
                'body': {'error': 'Not found'}
            }
    
    async def _handle_chat_completion(self, request: dict) -> dict:
        """处理聊天完成请求"""
        # 实际业务逻辑
        pass
    
    async def _handle_models(self, request: dict) -> dict:
        """处理模型列表请求"""
        pass
```

---

### 102-110: 各个中间件的集成测试

**新建**: `tests/gateway/test_middleware_integration.py`

```python
import pytest
import asyncio
from agent_manager.gateway.middleware import MiddlewareChain
from agent_manager.gateway.middleware.auth import AuthMiddleware
from agent_manager.gateway.middleware.validation import RequestValidationMiddleware
from agent_manager.gateway.middleware.logging import LoggingMiddleware


@pytest.mark.asyncio
async def test_middleware_chain():
    """测试中间件链"""
    chain = MiddlewareChain()
    
    # 添加中间件
    chain.use(LoggingMiddleware())
    chain.use(AuthMiddleware("test_key"))
    chain.use(RequestValidationMiddleware())
    
    # 最终处理器
    async def final_handler(request):
        return {'status': 200, 'body': {'result': 'ok'}}
    
    # 构建链
    handler = chain.build(final_handler)
    
    # 测试请求
    request = {
        'method': 'POST',
        'path': '/test',
        'headers': {
            'Authorization': 'Bearer test_key',
            'Content-Type': 'application/json'
        },
        'body': b'{"test": true}'
    }
    
    response = await handler(request)
    assert response['status'] == 200


@pytest.mark.asyncio
async def test_auth_middleware_rejection():
    """测试认证中间件拒绝无效请求"""
    middleware = AuthMiddleware("valid_key")
    
    async def next_handler(request):
        return {'status': 200}
    
    # 无认证头
    request = {'headers': {}}
    response = await middleware.process(request, next_handler)
    assert response['status'] == 401
    
    # 错误的 key
    request = {'headers': {'Authorization': 'Bearer wrong_key'}}
    response = await middleware.process(request, next_handler)
    assert response['status'] == 401
```

---

## 修复 111-120: 配置管理统一

### 111: 创建统一配置管理器

**新建**: `src/agent_manager/core/config_manager.py`

```python
"""统一配置管理器"""
from pathlib import Path
from typing import Any, Optional, Dict
import toml
import json
import os


class ConfigLayer:
    """配置层抽象"""
    
    def get(self, key: str) -> Optional[Any]:
        raise NotImplementedError
    
    def set(self, key: str, value: Any):
        raise NotImplementedError


class TOMLConfigLayer(ConfigLayer):
    """TOML 配置层"""
    
    def __init__(self, path: Path):
        self.path = path
        self._data = {}
        self.load()
    
    def load(self):
        """加载配置"""
        if self.path.exists():
            with open(self.path, 'r', encoding='utf-8') as f:
                self._data = toml.load(f)
    
    def save(self):
        """保存配置"""
        with open(self.path, 'w', encoding='utf-8') as f:
            toml.dump(self._data, f)
    
    def get(self, key: str) -> Optional[Any]:
        """获取配置值"""
        keys = key.split('.')
        current = self._data
        
        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return None
        
        return current
    
    def set(self, key: str, value: Any):
        """设置配置值"""
        keys = key.split('.')
        current = self._data
        
        for k in keys[:-1]:
            if k not in current:
                current[k] = {}
            current = current[k]
        
        current[keys[-1]] = value


class JSONSettingsLayer(ConfigLayer):
    """JSON 设置层"""
    
    def __init__(self, path: Path):
        self.path = path
        self._data = {}
        self.load()
    
    def load(self):
        if self.path.exists():
            with open(self.path, 'r', encoding='utf-8') as f:
                self._data = json.load(f)
    
    def save(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, indent=2)
    
    def get(self, key: str) -> Optional[Any]:
        return self._data.get(key)
    
    def set(self, key: str, value: Any):
        self._data[key] = value


class EnvConfigLayer(ConfigLayer):
    """环境变量层"""
    
    def get(self, key: str) -> Optional[Any]:
        """从环境变量获取"""
        env_key = key.upper().replace('.', '_')
        return os.environ.get(env_key)
    
    def set(self, key: str, value: Any):
        """设置环境变量"""
        env_key = key.upper().replace('.', '_')
        os.environ[env_key] = str(value)


class UnifiedConfigManager:
    """统一配置管理器"""
    
    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self.layers: list[ConfigLayer] = []
        self._setup_layers()
    
    def _setup_layers(self):
        """设置配置层（优先级从高到低）"""
        # 1. 环境变量（最高优先级）
        self.layers.append(EnvConfigLayer())
        
        # 2. 用户设置
        user_settings = self.config_dir / "settings.json"
        if user_settings.exists():
            self.layers.append(JSONSettingsLayer(user_settings))
        
        # 3. 项目配置
        project_config = self.config_dir / "config.toml"
        if project_config.exists():
            self.layers.append(TOMLConfigLayer(project_config))
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        for layer in self.layers:
            value = layer.get(key)
            if value is not None:
                return value
        return default
    
    def set(self, key: str, value: Any, layer: str = "toml"):
        """设置配置值"""
        if layer == "env":
            self.layers[0].set(key, value)
        elif layer == "json":
            self.layers[1].set(key, value)
            self.layers[1].save()
        elif layer == "toml":
            self.layers[2].set(key, value)
            self.layers[2].save()
    
    def get_all(self) -> Dict[str, Any]:
        """获取所有配置"""
        result = {}
        
        # 逆序遍历，优先级高的覆盖低的
        for layer in reversed(self.layers):
            if hasattr(layer, '_data'):
                result.update(layer._data)
        
        return result
```

---

### 112-120: 配置管理器测试和集成

**新建**: `tests/core/test_config_manager.py`

```python
import pytest
from pathlib import Path
from agent_manager.core.config_manager import UnifiedConfigManager, TOMLConfigLayer


@pytest.fixture
def temp_config_dir(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    
    # 创建测试配置文件
    toml_file = config_dir / "config.toml"
    toml_file.write_text("[api]\nkey = 'test_key'\nendpoint = 'https://api.example.com'")
    
    return config_dir


def test_config_manager_get(temp_config_dir):
    """测试获取配置"""
    manager = UnifiedConfigManager(temp_config_dir)
    
    value = manager.get("api.key")
    assert value == "test_key"


def test_config_manager_priority(temp_config_dir, monkeypatch):
    """测试配置优先级"""
    manager = UnifiedConfigManager(temp_config_dir)
    
    # TOML 中的值
    assert manager.get("api.key") == "test_key"
    
    # 设置环境变量（更高优先级）
    monkeypatch.setenv("API_KEY", "env_key")
    
    # 重新加载
    manager = UnifiedConfigManager(temp_config_dir)
    assert manager.get("api.key") == "env_key"


def test_config_manager_set(temp_config_dir):
    """测试设置配置"""
    manager = UnifiedConfigManager(temp_config_dir)
    
    manager.set("api.timeout", 30, layer="toml")
    
    value = manager.get("api.timeout")
    assert value == 30
```

---

**验证 091-120**:
```bash
# 测试 switching 模块
python -m pytest tests/switching/ -v

# 测试 app_server 模块
python -m pytest tests/app_server/ -v

# 测试 gateway 中间件
python -m pytest tests/gateway/ -v

# 测试配置管理器
python -m pytest tests/core/test_config_manager.py -v

# 完整测试
python -m pytest tests/ -v
```

---

**下一组 (121-150) 预览**:
- 测试补充（缺失模块的测试）
- 文档生成和完善
- 代码风格统一
- 性能优化
