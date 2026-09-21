# 修复 1-2: 速率限制器实现

## 1. 创建速率限制器模块

**文件**: `src/agent_manager/core/rate_limiter.py` (新建)

```python
"""速率限制器 - 防止暴力破解和资源耗尽攻击"""
import time
import threading
from collections import defaultdict
from typing import Dict, List


class RateLimiter:
    """基于令牌桶算法的速率限制器"""
    
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        """
        初始化速率限制器
        
        Args:
            max_requests: 时间窗口内允许的最大请求数
            window_seconds: 时间窗口大小（秒）
        """
        self.max_requests = max_requests
        self.window = window_seconds
        self.requests: Dict[str, List[float]] = defaultdict(list)
        self._lock = threading.Lock()
    
    def is_allowed(self, client_id: str) -> bool:
        """
        检查客户端是否允许发送请求
        
        Args:
            client_id: 客户端标识符（IP地址、会话ID等）
        
        Returns:
            True 如果允许，False 如果超过速率限制
        """
        with self._lock:
            now = time.time()
            
            # 清理过期的请求记录
            self.requests[client_id] = [
                req_time for req_time in self.requests[client_id]
                if now - req_time < self.window
            ]
            
            # 检查是否超过限制
            if len(self.requests[client_id]) < self.max_requests:
                self.requests[client_id].append(now)
                return True
            
            return False
    
    def get_remaining(self, client_id: str) -> int:
        """获取剩余可用请求数"""
        with self._lock:
            now = time.time()
            self.requests[client_id] = [
                req_time for req_time in self.requests[client_id]
                if now - req_time < self.window
            ]
            return max(0, self.max_requests - len(self.requests[client_id]))
    
    def reset(self, client_id: str):
        """重置客户端的速率限制记录"""
        with self._lock:
            if client_id in self.requests:
                del self.requests[client_id]


class IPRateLimiter(RateLimiter):
    """基于IP地址的速率限制器"""
    
    def extract_client_id(self, request_headers: dict) -> str:
        """从请求头提取客户端IP"""
        # 优先使用 X-Forwarded-For
        forwarded = request_headers.get('X-Forwarded-For', '')
        if forwarded:
            return forwarded.split(',')[0].strip()
        
        # 回退到 X-Real-IP
        real_ip = request_headers.get('X-Real-IP', '')
        if real_ip:
            return real_ip
        
        # 使用远程地址
        return request_headers.get('Remote-Addr', 'unknown')
```

**验证命令**:
```bash
python -c "from agent_manager.core.rate_limiter import RateLimiter; print('OK')"
```

---

## 2. 应用速率限制到 HTTP 服务器

**文件**: `src/agent_manager/application/http.py`

### 2.1 添加导入（文件顶部，约第10行后）

```python
from agent_manager.core.rate_limiter import IPRateLimiter
```

### 2.2 在 ManagerServer.__init__ 中初始化限制器

在 `__init__` 方法中添加：

```python
# 初始化速率限制器
self._oauth_rate_limiter = IPRateLimiter(max_requests=5, window_seconds=300)  # OAuth: 5次/5分钟
self._auth_rate_limiter = IPRateLimiter(max_requests=10, window_seconds=60)   # 认证: 10次/分钟
self._api_rate_limiter = IPRateLimiter(max_requests=100, window_seconds=60)   # API: 100次/分钟
```

### 2.3 添加速率限制检查方法

在 `ManagerServer` 类中添加：

```python
def _check_rate_limit(self, headers: dict, limiter: IPRateLimiter, limit_name: str) -> tuple:
    """
    检查速率限制
    
    Returns:
        (is_allowed, error_message)
    """
    client_id = limiter.extract_client_id(headers)
    
    if not limiter.is_allowed(client_id):
        return False, f"Rate limit exceeded for {limit_name}. Try again later."
    
    return True, ""
```

### 2.4 在 OAuth 回调处理中应用

在处理 OAuth 回调的方法开头添加（约 518 行附近）：

```python
# 速率限制检查
allowed, error_msg = self._check_rate_limit(
    self.headers, 
    self._oauth_rate_limiter, 
    "OAuth callback"
)
if not allowed:
    self.send_response(429)
    self.send_header('Content-Type', 'application/json')
    self.send_header('Retry-After', '300')
    self.end_headers()
    self.wfile.write(json.dumps({"error": error_msg}).encode())
    return
```

**验证命令**:
```bash
python -m pytest tests/integration/test_app_workbench_routes.py -v
```
