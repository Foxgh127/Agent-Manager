# 修复清单 081-090: 完成拆分准备和开始实际拆分

## 修复 081-085: Gateway 架构准备

### 081: 创建中间件基础架构

**新建**: `src/agent_manager/gateway/middleware/__init__.py`

```python
"""Gateway 中间件系统"""
from typing import Protocol, Callable, Awaitable, Any

Request = dict
Response = dict
Handler = Callable[[Request], Awaitable[Response]]


class Middleware(Protocol):
    """中间件协议"""
    
    async def process(self, request: Request, next_handler: Handler) -> Response:
        """处理请求并调用下一个处理器"""
        ...


class MiddlewareChain:
    """中间件链"""
    
    def __init__(self):
        self.middlewares: list[Middleware] = []
    
    def use(self, middleware: Middleware):
        """添加中间件"""
        self.middlewares.append(middleware)
    
    def build(self, final_handler: Handler) -> Handler:
        """构建中间件链"""
        handler = final_handler
        
        # 逆序构建链
        for middleware in reversed(self.middlewares):
            # 闭包捕获当前 handler
            def make_handler(mw, h):
                async def wrapped(req):
                    return await mw.process(req, h)
                return wrapped
            
            handler = make_handler(middleware, handler)
        
        return handler
```

---

### 082: 创建认证中间件

**新建**: `src/agent_manager/gateway/middleware/auth.py`

```python
"""认证中间件"""
from agent_manager.gateway.middleware import Middleware, Request, Response, Handler


class AuthMiddleware:
    """认证中间件"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
    
    async def process(self, request: Request, next_handler: Handler) -> Response:
        """验证请求认证"""
        auth_header = request.get('headers', {}).get('Authorization', '')
        
        if not auth_header:
            return {
                'status': 401,
                'body': {'error': 'Missing Authorization header'}
            }
        
        if not auth_header.startswith('Bearer '):
            return {
                'status': 401,
                'body': {'error': 'Invalid Authorization format'}
            }
        
        token = auth_header[7:]  # 移除 'Bearer '
        
        if token != self.api_key:
            return {
                'status': 401,
                'body': {'error': 'Invalid API key'}
            }
        
        # 认证通过，继续处理
        return await next_handler(request)


class APIKeyValidator:
    """API Key 验证器"""
    
    def __init__(self, valid_keys: set[str]):
        self.valid_keys = valid_keys
    
    def is_valid(self, key: str) -> bool:
        """检查 API Key 是否有效"""
        return key in self.valid_keys
    
    def add_key(self, key: str):
        """添加有效 Key"""
        self.valid_keys.add(key)
    
    def remove_key(self, key: str):
        """移除 Key"""
        self.valid_keys.discard(key)
```

---

### 083: 创建验证中间件

**新建**: `src/agent_manager/gateway/middleware/validation.py`

```python
"""请求验证中间件"""
from agent_manager.gateway.middleware import Middleware, Request, Response, Handler


class RequestValidationMiddleware:
    """请求验证中间件"""
    
    def __init__(self, max_body_size: int = 10 * 1024 * 1024):
        self.max_body_size = max_body_size
    
    async def process(self, request: Request, next_handler: Handler) -> Response:
        """验证请求"""
        # 验证方法
        method = request.get('method', '').upper()
        if method not in ['GET', 'POST', 'PUT', 'DELETE', 'PATCH']:
            return {
                'status': 405,
                'body': {'error': f'Method {method} not allowed'}
            }
        
        # 验证内容类型
        if method in ['POST', 'PUT', 'PATCH']:
            content_type = request.get('headers', {}).get('Content-Type', '')
            if not content_type:
                return {
                    'status': 400,
                    'body': {'error': 'Content-Type header required'}
                }
        
        # 验证 body 大小
        body = request.get('body', b'')
        if isinstance(body, bytes) and len(body) > self.max_body_size:
            return {
                'status': 413,
                'body': {'error': 'Request body too large'}
            }
        
        return await next_handler(request)


class ModelValidationMiddleware:
    """模型请求验证中间件"""
    
    def __init__(self, allowed_models: set[str]):
        self.allowed_models = allowed_models
    
    async def process(self, request: Request, next_handler: Handler) -> Response:
        """验证模型请求"""
        import json
        
        # 解析请求体
        body = request.get('body', b'')
        if isinstance(body, bytes):
            try:
                data = json.loads(body.decode('utf-8'))
            except:
                return {
                    'status': 400,
                    'body': {'error': 'Invalid JSON'}
                }
        else:
            data = body
        
        # 验证模型
        model = data.get('model', '')
        if model and self.allowed_models and model not in self.allowed_models:
            return {
                'status': 400,
                'body': {'error': f'Model {model} not allowed'}
            }
        
        return await next_handler(request)
```

---

### 084: 创建日志中间件

**新建**: `src/agent_manager/gateway/middleware/logging.py`

```python
"""日志中间件"""
import time
import logging
from agent_manager.gateway.middleware import Middleware, Request, Response, Handler


class LoggingMiddleware:
    """日志中间件"""
    
    def __init__(self, logger: logging.Logger = None):
        self.logger = logger or logging.getLogger(__name__)
    
    async def process(self, request: Request, next_handler: Handler) -> Response:
        """记录请求和响应"""
        start_time = time.time()
        
        # 记录请求
        method = request.get('method', 'UNKNOWN')
        path = request.get('path', '/')
        self.logger.info(f"→ {method} {path}")
        
        try:
            response = await next_handler(request)
            
            # 记录响应
            duration = time.time() - start_time
            status = response.get('status', 200)
            self.logger.info(f"← {status} {method} {path} ({duration:.3f}s)")
            
            return response
        
        except Exception as e:
            duration = time.time() - start_time
            self.logger.error(f"✗ {method} {path} ({duration:.3f}s): {str(e)}")
            raise


class RequestIDMiddleware:
    """请求 ID 中间件"""
    
    def __init__(self):
        self.counter = 0
    
    async def process(self, request: Request, next_handler: Handler) -> Response:
        """为每个请求分配 ID"""
        import uuid
        
        request_id = str(uuid.uuid4())
        request['request_id'] = request_id
        
        response = await next_handler(request)
        response.setdefault('headers', {})['X-Request-ID'] = request_id
        
        return response
```

---

### 085: 创建速率限制中间件

**新建**: `src/agent_manager/gateway/middleware/rate_limit.py`

```python
"""速率限制中间件"""
from agent_manager.gateway.middleware import Middleware, Request, Response, Handler
from agent_manager.core.rate_limiter import RateLimiter


class RateLimitMiddleware:
    """速率限制中间件"""
    
    def __init__(self, max_requests: int = 100, window_seconds: int = 60):
        self.limiter = RateLimiter(max_requests, window_seconds)
    
    def _extract_client_id(self, request: Request) -> str:
        """提取客户端标识"""
        headers = request.get('headers', {})
        
        # 优先使用 API Key
        auth = headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            return auth[7:20]  # 使用 Key 的前13个字符作为 ID
        
        # 回退到 IP
        return headers.get('X-Forwarded-For', 'unknown').split(',')[0].strip()
    
    async def process(self, request: Request, next_handler: Handler) -> Response:
        """检查速率限制"""
        client_id = self._extract_client_id(request)
        
        if not self.limiter.is_allowed(client_id):
            remaining = self.limiter.get_remaining(client_id)
            return {
                'status': 429,
                'headers': {
                    'X-RateLimit-Remaining': str(remaining),
                    'Retry-After': str(self.limiter.window)
                },
                'body': {'error': 'Rate limit exceeded'}
            }
        
        response = await next_handler(request)
        
        # 添加速率限制头
        remaining = self.limiter.get_remaining(client_id)
        response.setdefault('headers', {})['X-RateLimit-Remaining'] = str(remaining)
        
        return response
```

---

## 修复 086-090: 开始实际拆分 runtime.py

### 086: 创建 runtime 包结构

**新建**: `src/agent_manager/core/runtime/__init__.py`

```python
"""Runtime 模块 - 统一入口"""

# 导入所有公共 API
from .nodejs_discovery import (
    find_nodejs_installation,
    find_npm_installation,
    get_nodejs_version,
    check_nodejs_requirements
)

from .windows_registry import (
    read_registry_value,
    find_installed_programs,
    find_codex_installation_from_registry
)

from .powershell_executor import (
    run_powershell_command,
    run_powershell_script,
    get_windows_version,
    check_winget_available
)

from .tarball_handler import (
    download_tarball,
    extract_tarball,
    download_and_extract_tarball
)

from .version_utils import (
    parse_version,
    compare_versions,
    is_version_compatible,
    get_latest_version
)

# 导入主要的运行时管理函数
from .installation import (
    download_official_codex_runtime,
    install_codex_runtime,
    verify_installation
)

from .discovery import (
    find_codex_executable,
    detect_codex_installation,
    get_codex_version
)

from .validation import (
    validate_codex_installation,
    check_runtime_health
)

__all__ = [
    # Node.js
    'find_nodejs_installation',
    'find_npm_installation',
    'get_nodejs_version',
    'check_nodejs_requirements',
    
    # Registry
    'read_registry_value',
    'find_installed_programs',
    'find_codex_installation_from_registry',
    
    # PowerShell
    'run_powershell_command',
    'run_powershell_script',
    'get_windows_version',
    'check_winget_available',
    
    # Tarball
    'download_tarball',
    'extract_tarball',
    'download_and_extract_tarball',
    
    # Version
    'parse_version',
    'compare_versions',
    'is_version_compatible',
    'get_latest_version',
    
    # Installation
    'download_official_codex_runtime',
    'install_codex_runtime',
    'verify_installation',
    
    # Discovery
    'find_codex_executable',
    'detect_codex_installation',
    'get_codex_version',
    
    # Validation
    'validate_codex_installation',
    'check_runtime_health',
]
```

---

### 087: 创建 installation.py（从 runtime.py 提取）

**新建**: `src/agent_manager/core/runtime/installation.py`

```python
"""Codex 运行时安装"""
from pathlib import Path
from typing import Optional, Tuple
from agent_manager.core.runtime.tarball_handler import download_and_extract_tarball
from agent_manager.core.http_client import create_api_client


def get_official_runtime_metadata() -> dict:
    """获取官方运行时元数据"""
    # 这里应该是实际的元数据 URL
    metadata_url = "https://api.codex.example.com/runtime/metadata"
    
    client = create_api_client()
    return client.get_json(metadata_url)


def download_official_codex_runtime(
    version: str,
    destination: Path,
    platform: str = "win32-x64"
) -> Tuple[bool, str]:
    """
    下载官方 Codex 运行时
    
    Returns:
        (success, message)
    """
    try:
        metadata = get_official_runtime_metadata()
        
        # 查找对应版本和平台的下载链接
        runtime_info = None
        for runtime in metadata.get('runtimes', []):
            if runtime['version'] == version and runtime['platform'] == platform:
                runtime_info = runtime
                break
        
        if not runtime_info:
            return False, f"Runtime not found for version {version} platform {platform}"
        
        download_url = runtime_info['download_url']
        
        # 下载并解压
        success = download_and_extract_tarball(download_url, destination)
        
        if success:
            return True, f"Runtime downloaded to {destination}"
        else:
            return False, "Download failed"
    
    except Exception as e:
        return False, f"Download error: {str(e)}"


def install_codex_runtime(
    runtime_dir: Path,
    install_to: Path
) -> Tuple[bool, str]:
    """
    安装 Codex 运行时
    
    Args:
        runtime_dir: 解压的运行时目录
        install_to: 安装目标目录
    
    Returns:
        (success, message)
    """
    try:
        install_to.mkdir(parents=True, exist_ok=True)
        
        # 查找可执行文件
        exe_candidates = list(runtime_dir.rglob("codex*.exe"))
        if not exe_candidates:
            return False, "Codex executable not found in runtime"
        
        codex_exe = exe_candidates[0]
        
        # 复制文件
        import shutil
        shutil.copytree(runtime_dir, install_to, dirs_exist_ok=True)
        
        # 验证安装
        installed_exe = install_to / codex_exe.name
        if not installed_exe.exists():
            return False, "Installation verification failed"
        
        return True, f"Installed to {install_to}"
    
    except Exception as e:
        return False, f"Installation error: {str(e)}"


def verify_installation(codex_path: Path) -> Tuple[bool, str]:
    """
    验证 Codex 安装
    
    Returns:
        (is_valid, message)
    """
    if not codex_path.exists():
        return False, "Codex executable not found"
    
    if not codex_path.is_file():
        return False, "Codex path is not a file"
    
    # 尝试运行 --version
    import subprocess
    try:
        result = subprocess.run(
            [str(codex_path), "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            version = result.stdout.strip()
            return True, f"Codex {version} is installed"
        else:
            return False, "Codex version check failed"
    
    except Exception as e:
        return False, f"Verification error: {str(e)}"
```

---

### 088: 创建 discovery.py（从 runtime.py 提取）

**新建**: `src/agent_manager/core/runtime/discovery.py`

```python
"""Codex 可执行文件发现"""
from pathlib import Path
from typing import Optional, List
import subprocess


def find_codex_executable() -> Optional[Path]:
    """查找 Codex 可执行文件"""
    # 方法 1: 从 PATH 查找
    try:
        result = subprocess.run(
            ["where", "codex"],
            capture_output=True,
            text=True,
            check=False
        )
        
        if result.returncode == 0 and result.stdout:
            return Path(result.stdout.strip().split('\n')[0])
    except:
        pass
    
    # 方法 2: 从常见安装位置查找
    common_locations = [
        Path.home() / ".codex" / "codex.exe",
        Path("C:/Program Files/Codex/codex.exe"),
        Path("C:/Program Files (x86)/Codex/codex.exe"),
    ]
    
    for location in common_locations:
        if location.exists():
            return location
    
    # 方法 3: 从注册表查找
    from agent_manager.core.runtime.windows_registry import find_codex_installation_from_registry
    registry_path = find_codex_installation_from_registry()
    if registry_path:
        codex_exe = registry_path / "codex.exe"
        if codex_exe.exists():
            return codex_exe
    
    return None


def detect_codex_installation() -> List[Path]:
    """检测所有 Codex 安装"""
    installations = []
    
    # 从 PATH 查找所有实例
    try:
        result = subprocess.run(
            ["where", "codex"],
            capture_output=True,
            text=True,
            check=False
        )
        
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line:
                    installations.append(Path(line))
    except:
        pass
    
    # 从常见位置查找
    common_locations = [
        Path.home() / ".codex",
        Path("C:/Program Files/Codex"),
        Path("C:/Program Files (x86)/Codex"),
    ]
    
    for location in common_locations:
        if location.exists():
            for exe in location.rglob("codex*.exe"):
                if exe not in installations:
                    installations.append(exe)
    
    return installations


def get_codex_version(codex_path: Path) -> Optional[str]:
    """获取 Codex 版本"""
    try:
        result = subprocess.run(
            [str(codex_path), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False
        )
        
        if result.returncode == 0:
            return result.stdout.strip()
    except:
        pass
    
    return None
```

---

### 089: 创建 validation.py（从 runtime.py 提取）

**新建**: `src/agent_manager/core/runtime/validation.py`

```python
"""Codex 运行时验证"""
from pathlib import Path
from typing import Tuple, List
from agent_manager.platform.paths import is_windows_store_path


def validate_codex_installation(codex_path: Path) -> Tuple[bool, List[str]]:
    """
    验证 Codex 安装
    
    Returns:
        (is_valid, list_of_issues)
    """
    issues = []
    
    # 检查文件存在
    if not codex_path.exists():
        issues.append("Codex executable does not exist")
        return False, issues
    
    if not codex_path.is_file():
        issues.append("Codex path is not a file")
        return False, issues
    
    # 检查是否为 Windows Store 路径
    if is_windows_store_path(codex_path):
        issues.append("Codex installed via Windows Store may have limitations")
    
    # 检查可执行权限
    import os
    if not os.access(codex_path, os.X_OK):
        issues.append("Codex executable does not have execute permission")
    
    # 检查版本
    from agent_manager.core.runtime.discovery import get_codex_version
    version = get_codex_version(codex_path)
    if not version:
        issues.append("Cannot determine Codex version")
    
    # 检查依赖
    dependencies = check_codex_dependencies(codex_path)
    if not dependencies[0]:
        issues.append(f"Missing dependencies: {dependencies[1]}")
    
    return len(issues) == 0, issues


def check_codex_dependencies(codex_path: Path) -> Tuple[bool, str]:
    """检查 Codex 依赖"""
    # 检查 Node.js
    from agent_manager.core.runtime.nodejs_discovery import find_nodejs_installation
    if not find_nodejs_installation():
        return False, "Node.js not found"
    
    # 检查其他依赖...
    
    return True, ""


def check_runtime_health() -> dict:
    """检查运行时健康状况"""
    from agent_manager.core.runtime.discovery import find_codex_executable, get_codex_version
    from agent_manager.core.runtime.nodejs_discovery import (
        find_nodejs_installation,
        get_nodejs_version
    )
    
    health = {
        'codex': {
            'installed': False,
            'path': None,
            'version': None
        },
        'nodejs': {
            'installed': False,
            'path': None,
            'version': None
        },
        'overall': 'unknown'
    }
    
    # 检查 Codex
    codex_path = find_codex_executable()
    if codex_path:
        health['codex']['installed'] = True
        health['codex']['path'] = str(codex_path)
        health['codex']['version'] = get_codex_version(codex_path)
    
    # 检查 Node.js
    node_path = find_nodejs_installation()
    if node_path:
        health['nodejs']['installed'] = True
        health['nodejs']['path'] = str(node_path)
        health['nodejs']['version'] = get_nodejs_version(node_path)
    
    # 综合健康状态
    if health['codex']['installed'] and health['nodejs']['installed']:
        health['overall'] = 'healthy'
    elif health['codex']['installed']:
        health['overall'] = 'degraded'
    else:
        health['overall'] = 'unhealthy'
    
    return health
```

---

### 090: 更新 core/runtime.py 使用新模块

**文件**: `src/agent_manager/core/runtime.py`

现在这个文件应该大大简化，主要作为向后兼容的入口：

```python
"""
Runtime 模块 - 向后兼容入口

新代码应该使用 agent_manager.core.runtime 包中的具体模块。
"""

# 向后兼容：重新导出所有函数
from agent_manager.core.runtime import *

# 保留原有的任何特殊逻辑或不容易迁移的函数
# （如果有的话）

# 标记为废弃
import warnings
warnings.warn(
    "Direct import from runtime.py is deprecated. "
    "Use agent_manager.core.runtime package instead.",
    DeprecationWarning,
    stacklevel=2
)
```

---

**验证 081-090**:
```bash
# 测试中间件
python -m pytest tests/gateway/ -v

# 测试 runtime 拆分
python -c "from agent_manager.core.runtime import find_codex_executable; print('OK')"

# 运行所有测试
python -m pytest tests/core/ tests/gateway/ -v
```

---

## 下一步 (091-120)

在下一组中，我们将：
- 继续拆分 switching.py（创建账号切换器、启动协调器）
- 拆分 app_server.py（创建会话管理器）
- 重构 Gateway 使用新的中间件架构
