# Agent Manager API 文档

**版本**: 1.3.0  
**最后更新**: 2026-09-21

---

## 核心模块

### HTTP 客户端

#### SafeHTTPClient

统一的 HTTP 客户端，具有安全特性和资源管理。

```python
from agent_manager.core.http_client import SafeHTTPClient

client = SafeHTTPClient(timeout=30, max_response_bytes=10_000_000)

# GET 请求
data = client.get("https://api.example.com/data")

# POST 请求
response = client.post_json("https://api.example.com/create", {"name": "test"})

# 流式打开
with client.open_stream("https://example.com/large-file") as response:
    for chunk in response.iter_content(chunk_size=8192):
        process(chunk)
```

**特性**:
- 自动资源清理
- 响应大小限制
- 同源重定向处理
- 请求超时控制

---

### URL 验证

#### validate_url()

验证和规范化 URL，防止 SSRF 攻击。

```python
from agent_manager.core.url_validator import validate_url, URLValidationError

try:
    safe_url = validate_url(
        "https://api.example.com/endpoint",
        allowed_schemes={"https"},
        allow_loopback=False
    )
except URLValidationError as e:
    print(f"Invalid URL: {e}")
```

**参数**:
- `url`: 要验证的 URL
- `allowed_schemes`: 允许的协议（默认: `{"https"}`）
- `allow_loopback`: 是否允许回环地址（默认: `False`）
- `allow_private_networks`: 是否允许私有网络（默认: `False`）
- `allowed_ports`: 允许的端口列表

---

### 速率限制

#### RateLimiter

滑动窗口速率限制器。

```python
from agent_manager.core.rate_limiter import RateLimiter

limiter = RateLimiter(max_requests=100, window_seconds=60)

# 检查是否允许
if limiter.is_allowed("client_id"):
    # 处理请求
    pass
else:
    # 拒绝请求
    remaining = limiter.get_remaining("client_id")
    print(f"Rate limit exceeded. Try again in {remaining}s")
```

**特性**:
- 滑动窗口算法
- 线程安全
- 自动内存管理
- 代理支持

---

### 国际化

#### t()

翻译函数，支持多语言。

```python
from agent_manager.core.i18n import t, set_locale

# 设置语言
set_locale("zh-CN")

# 获取翻译
error_msg = t("errors.account_not_found")

# 带参数的翻译
msg = t("errors.batch_request_range", min=1, max=200)
```

**支持的语言**:
- `zh-CN`: 简体中文
- `en-US`: 英文

---

## App Server 模块

### codex_app_server_request()

向 Codex App Server 发送单个请求。

```python
from agent_manager.core.app_server import codex_app_server_request

result = codex_app_server_request(
    "thread/list",
    {"limit": 50, "archived": False},
    timeout=30
)
```

### codex_app_server_requests()

批量请求。

```python
from agent_manager.core.app_server import codex_app_server_requests

requests = [
    ("thread/list", {"limit": 10}),
    ("account/read", {}),
]

results = codex_app_server_requests(requests, timeout=60)
```

### list_codex_thread_groups()

列出 Codex 线程。

```python
from agent_manager.core.app_server import list_codex_thread_groups

threads = list_codex_thread_groups(
    active_limit=500,
    archived_limit=500,
    rebuild=False
)

for thread in threads:
    print(f"{thread['name']}: {thread['status']['type']}")
```

### manage_codex_threads()

管理线程（归档、恢复、删除等）。

```python
from agent_manager.core.app_server import manage_codex_threads

result = manage_codex_threads("archive", ["thread_id_1", "thread_id_2"])
print(f"Archived {result['succeeded']} threads")
```

---

## Runtime 模块

### codex_prefix()

获取 Codex 可执行文件路径。

```python
from agent_manager.core.runtime import codex_prefix

prefix = codex_prefix()
print(f"Codex path: {prefix[0]}")
```

### codex_runtime_status()

检查运行时状态。

```python
from agent_manager.core.runtime import codex_runtime_status

status = codex_runtime_status()
print(f"Runtime: {status['source']}")
print(f"Version: {status.get('version')}")
```

### resolve_codex_launch_plan()

解析启动计划。

```python
from agent_manager.core.runtime import resolve_codex_launch_plan

plan = resolve_codex_launch_plan()
print(f"Workspace: {plan.get('workspace')}")
```

---

## Switching 模块

### switch_codex_account()

切换 Codex 账号。

```python
from agent_manager.core.switching import switch_codex_account

result = switch_codex_account(
    target_account_id="account_123",
    auto_launch=True
)
```

---

## 工具函数

### 进程管理

```python
from agent_manager.core.process_utils import detect_codex_processes, ProcessCheckMode

# 检测 Codex 进程
processes = detect_codex_processes(mode=ProcessCheckMode.THOROUGH)

for proc in processes:
    print(f"PID: {proc.pid}, Path: {proc.exe_path}")
```

### 平台路径检查

```python
from agent_manager.platform.paths import is_windows_store_path
from pathlib import Path

path = Path("C:/Program Files/WindowsApps/...")
if is_windows_store_path(path):
    print("This is a Windows Store app path")
```

---

## 错误处理

### 常见异常

```python
from agent_manager.core import ManagerError
from agent_manager.core.url_validator import URLValidationError

try:
    # 操作代码
    pass
except URLValidationError as e:
    # URL 验证失败
    print(f"URL validation failed: {e}")
except ManagerError as e:
    # Agent Manager 错误
    print(f"Manager error: {e}")
```

---

## 配置管理

### 环境变量

支持通过环境变量覆盖配置：

```bash
# Windows PowerShell
$env:API_KEY="your_key"
$env:API_ENDPOINT="https://api.example.com"
```

---

## 完整示例

### 批量处理线程

```python
from agent_manager.core.app_server import (
    list_codex_thread_groups,
    manage_codex_threads
)
from agent_manager.core.rate_limiter import RateLimiter

# 创建速率限制器
limiter = RateLimiter(max_requests=50, window_seconds=60)

def process_threads():
    if not limiter.is_allowed("batch_processor"):
        print("Rate limit exceeded")
        return

    # 获取所有线程
    threads = list_codex_thread_groups(active_limit=100)

    # 找出旧线程
    old_threads = [
        t["id"] for t in threads
        if t.get("ageSeconds", 0) > 86400  # 24小时
    ]

    if old_threads:
        # 批量归档
        result = manage_codex_threads("archive", old_threads)
        print(f"Archived {result['succeeded']} old threads")

process_threads()
```

---

## 性能优化

### 批量操作

优先使用批量API：

```python
# ✗ 不推荐：逐个请求
for thread_id in thread_ids:
    codex_app_server_request("thread/archive", {"threadId": thread_id})

# ✓ 推荐：批量请求
requests = [("thread/archive", {"threadId": tid}) for tid in thread_ids]
codex_app_server_requests(requests)
```

### 资源清理

使用上下文管理器：

```python
# ✓ 推荐：自动清理
with client.open_stream(url) as response:
    process(response)

# ✗ 不推荐：手动清理
response = client.get(url)
try:
    process(response)
finally:
    response.close()
```

---

## 安全最佳实践

1. **始终验证 URL**
   ```python
   url = validate_url(user_input, allowed_schemes={"https"})
   ```

2. **使用速率限制**
   ```python
   if not limiter.is_allowed(client_id):
       raise ManagerError("Rate limit exceeded")
   ```

3. **限制响应大小**
   ```python
   client = SafeHTTPClient(max_response_bytes=10_000_000)
   ```

4. **使用国际化**
   ```python
   error = t("errors.invalid_input")  # 不要硬编码消息
   ```

---

## 故障排查

### 导入错误

```bash
# 确认安装
pip install -e .

# 测试导入
python -c "from agent_manager.core import http_client; print('OK')"
```

### 速率限制问题

```python
# 检查剩余配额
remaining = limiter.get_remaining(client_id)
print(f"Remaining requests: {remaining}")

# 重置配额
limiter.reset(client_id)
```

---

更多信息请参考：
- [架构文档](architecture.md)
- [开发指南](../development.md)
- [示例代码](../examples/)
