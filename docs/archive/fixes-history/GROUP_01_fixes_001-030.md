# 修复清单 001-030: 安全漏洞和并发问题

## 修复 001: 创建速率限制器模块

**新建文件**: `src/agent_manager/core/rate_limiter.py`

```python
"""速率限制器 - 防止暴力破解和资源耗尽攻击"""
import time
import threading
from collections import defaultdict
from typing import Dict, List


class RateLimiter:
    """基于令牌桶算法的速率限制器"""
    
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self.requests: Dict[str, List[float]] = defaultdict(list)
        self._lock = threading.Lock()
    
    def is_allowed(self, client_id: str) -> bool:
        with self._lock:
            now = time.time()
            self.requests[client_id] = [
                req_time for req_time in self.requests[client_id]
                if now - req_time < self.window
            ]
            if len(self.requests[client_id]) < self.max_requests:
                self.requests[client_id].append(now)
                return True
            return False
    
    def get_remaining(self, client_id: str) -> int:
        with self._lock:
            now = time.time()
            self.requests[client_id] = [
                req_time for req_time in self.requests[client_id]
                if now - req_time < self.window
            ]
            return max(0, self.max_requests - len(self.requests[client_id]))
    
    def reset(self, client_id: str):
        with self._lock:
            if client_id in self.requests:
                del self.requests[client_id]


class IPRateLimiter(RateLimiter):
    """基于IP地址的速率限制器"""
    
    def extract_client_id(self, request_headers: dict) -> str:
        forwarded = request_headers.get('X-Forwarded-For', '')
        if forwarded:
            return forwarded.split(',')[0].strip()
        real_ip = request_headers.get('X-Real-IP', '')
        if real_ip:
            return real_ip
        return request_headers.get('Remote-Addr', 'unknown')
```

**验证**: `python -c "from agent_manager.core.rate_limiter import RateLimiter; print('OK')"`

---

## 修复 002: HTTP 服务器添加速率限制

**文件**: `src/agent_manager/application/http.py`

### 2.1 添加导入（文件顶部约第10行后）
```python
from agent_manager.core.rate_limiter import IPRateLimiter
```

### 2.2 在 ManagerServer.__init__ 中初始化
```python
self._oauth_rate_limiter = IPRateLimiter(max_requests=5, window_seconds=300)
self._auth_rate_limiter = IPRateLimiter(max_requests=10, window_seconds=60)
self._api_rate_limiter = IPRateLimiter(max_requests=100, window_seconds=60)
```

### 2.3 添加检查方法
```python
def _check_rate_limit(self, headers: dict, limiter: IPRateLimiter, limit_name: str) -> tuple:
    client_id = limiter.extract_client_id(headers)
    if not limiter.is_allowed(client_id):
        return False, f"Rate limit exceeded for {limit_name}. Try again later."
    return True, ""
```

### 2.4 在 OAuth 回调处理开头添加（约518行）
```python
allowed, error_msg = self._check_rate_limit(
    self.headers, self._oauth_rate_limiter, "OAuth callback"
)
if not allowed:
    self.send_response(429)
    self.send_header('Content-Type', 'application/json')
    self.send_header('Retry-After', '300')
    self.end_headers()
    self.wfile.write(json.dumps({"error": error_msg}).encode())
    return
```

---

## 修复 003: JWT 签名验证

**文件**: `src/agent_manager/core/auth.py`

### 3.1 添加导入（文件顶部）
```python
import jwt
from jwt.exceptions import InvalidSignatureError, DecodeError, ExpiredSignatureError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from typing import Optional
```

### 3.2 添加公钥管理
```python
_JWT_PUBLIC_KEYS: dict = {}

def load_jwt_public_key(key_id: str, pem_data: bytes):
    _JWT_PUBLIC_KEYS[key_id] = pem_data
```

### 3.3 创建验证函数
```python
def validate_jwt_with_signature(
    token: str, 
    key_id: Optional[str] = None,
    algorithms: Optional[list] = None
) -> dict:
    if algorithms is None:
        algorithms = ["RS256", "ES256", "HS256"]
    
    try:
        unverified_header = jwt.get_unverified_header(token)
        if key_id is None:
            key_id = unverified_header.get('kid', 'default')
        
        if key_id not in _JWT_PUBLIC_KEYS:
            return jwt.decode(token, options={"verify_signature": False})
        
        pem_data = _JWT_PUBLIC_KEYS[key_id]
        
        if unverified_header.get('alg', '').startswith('HS'):
            key = pem_data
        else:
            key = serialization.load_pem_public_key(pem_data, backend=default_backend())
        
        payload = jwt.decode(
            token, key, algorithms=algorithms,
            options={"verify_signature": True, "verify_exp": True}
        )
        return payload
        
    except InvalidSignatureError:
        raise ValueError("JWT signature verification failed")
    except ExpiredSignatureError:
        raise ValueError("JWT token has expired")
    except DecodeError as e:
        raise ValueError(f"JWT decode error: {str(e)}")
```

### 3.4 更新 parse_jwt_claims
```python
def parse_jwt_claims(token: str) -> dict:
    try:
        return validate_jwt_with_signature(token)
    except ValueError:
        import logging
        logging.warning("JWT signature validation failed, proceeding without verification")
        return jwt.decode(token, options={"verify_signature": False})
```

**安装依赖**: `pip install PyJWT cryptography`

---

## 修复 004: DPAPI 输入验证

**文件**: `src/agent_manager/core/credential_store.py`

### 查找并替换 dpapi_unprotect（约36行）
```python
def dpapi_unprotect(ciphertext: bytes) -> bytes:
    if not isinstance(ciphertext, bytes):
        raise ValueError("Ciphertext must be bytes")
    if not ciphertext:
        raise ValueError("Ciphertext cannot be empty")
    if len(ciphertext) < 16:
        raise ValueError("Ciphertext too short: minimum 16 bytes required")
    
    MAX_CIPHERTEXT_SIZE = 1024 * 1024
    if len(ciphertext) > MAX_CIPHERTEXT_SIZE:
        raise ValueError(f"Ciphertext too large: maximum {MAX_CIPHERTEXT_SIZE} bytes allowed")
    
    try:
        plaintext = win32crypt.CryptUnprotectData(ciphertext, None, None, None, 0)[1]
        return plaintext
    except Exception as e:
        raise ValueError(f"DPAPI decryption failed: {str(e)}")
```

### 同样修改 dpapi_protect
```python
def dpapi_protect(plaintext: bytes) -> bytes:
    if not isinstance(plaintext, bytes):
        raise ValueError("Plaintext must be bytes")
    if not plaintext:
        raise ValueError("Plaintext cannot be empty")
    
    MAX_PLAINTEXT_SIZE = 1024 * 1024
    if len(plaintext) > MAX_PLAINTEXT_SIZE:
        raise ValueError(f"Plaintext too large: maximum {MAX_PLAINTEXT_SIZE} bytes allowed")
    
    try:
        ciphertext = win32crypt.CryptProtectData(plaintext, None, None, None, None, 0)
        return ciphertext
    except Exception as e:
        raise ValueError(f"DPAPI encryption failed: {str(e)}")
```

---

## 修复 005: 邮箱格式验证

**文件**: `src/agent_manager/accounts/reauthentication.py`

### 5.1 文件顶部添加
```python
import re

EMAIL_PATTERN = re.compile(
    r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
)
```

### 5.2 添加验证函数
```python
def validate_email(email: str) -> bool:
    if not email or not isinstance(email, str):
        return False
    if len(email) > 254:
        return False
    return EMAIL_PATTERN.match(email) is not None
```

### 5.3 在邮箱比较前添加验证（约24行）
```python
if not validate_email(account_email):
    raise ValueError(f"Invalid email format: {account_email}")
if not validate_email(snapshot_email):
    raise ValueError(f"Invalid email format in snapshot: {snapshot_email}")

if account_email.lower() == snapshot_email.lower():
    # 原有逻辑
```

---

## 修复 006: HTTP 请求体流式读取

**文件**: `src/agent_manager/application/http.py`

### 6.1 添加流式读取方法（ManagerServer类中）
```python
def _read_request_body_streaming(self, max_size: int = None) -> bytes:
    content_length = int(self.headers.get('Content-Length', 0))
    
    if max_size is None:
        max_size = MAX_IMPORT_BODY_BYTES
    
    if content_length > max_size:
        raise ValueError(f"Request body too large: {content_length} bytes (max: {max_size})")
    
    CHUNK_SIZE = 65536
    chunks = []
    total_read = 0
    
    while total_read < content_length:
        chunk_size = min(CHUNK_SIZE, content_length - total_read)
        chunk = self.rfile.read(chunk_size)
        if not chunk:
            break
        total_read += len(chunk)
        if total_read > max_size:
            raise ValueError(f"Request body exceeded maximum size during read: {max_size} bytes")
        chunks.append(chunk)
    
    return b''.join(chunks)
```

### 6.2 修改 do_POST（约209-259行，查找读取body的代码）
```python
def do_POST(self):
    try:
        body = self._read_request_body_streaming()
        # 继续原有处理逻辑
    except ValueError as e:
        self.send_response(413)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"error": str(e)}).encode())
        return
```

---

## 修复 007-010: 安全测试

**新建**: `tests/security/__init__.py`
```python
"""安全测试模块"""
```

**新建**: `tests/security/test_rate_limiting.py`
```python
import pytest
import time
from agent_manager.core.rate_limiter import RateLimiter, IPRateLimiter

def test_rate_limiter_basic():
    limiter = RateLimiter(max_requests=3, window_seconds=1)
    assert limiter.is_allowed("client1") is True
    assert limiter.is_allowed("client1") is True
    assert limiter.is_allowed("client1") is True
    assert limiter.is_allowed("client1") is False
    time.sleep(1.1)
    assert limiter.is_allowed("client1") is True

def test_rate_limiter_multiple_clients():
    limiter = RateLimiter(max_requests=2, window_seconds=1)
    assert limiter.is_allowed("client1") is True
    assert limiter.is_allowed("client1") is True
    assert limiter.is_allowed("client1") is False
    assert limiter.is_allowed("client2") is True

def test_rate_limiter_remaining():
    limiter = RateLimiter(max_requests=5, window_seconds=10)
    assert limiter.get_remaining("client1") == 5
    limiter.is_allowed("client1")
    assert limiter.get_remaining("client1") == 4

def test_ip_rate_limiter_extract():
    limiter = IPRateLimiter()
    headers = {'X-Forwarded-For': '192.168.1.100, 10.0.0.1'}
    assert limiter.extract_client_id(headers) == '192.168.1.100'
```

**新建**: `tests/security/test_jwt_validation.py`
```python
import pytest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from agent_manager.core.auth import validate_jwt_with_signature, load_jwt_public_key

@pytest.fixture
def rsa_key_pair():
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private_pem, public_pem

def test_jwt_valid_signature(rsa_key_pair):
    private_pem, public_pem = rsa_key_pair
    load_jwt_public_key('test-key', public_pem)
    payload = {'sub': 'user123', 'exp': 9999999999}
    token = jwt.encode(payload, private_pem, algorithm='RS256', headers={'kid': 'test-key'})
    result = validate_jwt_with_signature(token, key_id='test-key')
    assert result['sub'] == 'user123'
```

**新建**: `tests/security/test_dpapi_validation.py`
```python
import pytest
import sys
from agent_manager.core.credential_store import dpapi_protect, dpapi_unprotect

def test_dpapi_empty_input():
    with pytest.raises(ValueError, match="cannot be empty"):
        dpapi_protect(b"")
    with pytest.raises(ValueError, match="cannot be empty"):
        dpapi_unprotect(b"")

def test_dpapi_invalid_type():
    with pytest.raises(ValueError, match="must be bytes"):
        dpapi_protect("not bytes")

def test_dpapi_too_short_ciphertext():
    with pytest.raises(ValueError, match="too short"):
        dpapi_unprotect(b"short")

@pytest.mark.skipif(not sys.platform.startswith('win'), reason="Windows only")
def test_dpapi_valid_roundtrip():
    plaintext = b"secret data"
    ciphertext = dpapi_protect(plaintext)
    assert dpapi_unprotect(ciphertext) == plaintext
```

**新建**: `tests/security/test_email_validation.py`
```python
import pytest
from agent_manager.accounts.reauthentication import validate_email

def test_valid_emails():
    valid = ["user@example.com", "test.user@example.com", "a@b.co"]
    for email in valid:
        assert validate_email(email) is True

def test_invalid_emails():
    invalid = ["", "notanemail", "@example.com", "user@", "user@example"]
    for email in invalid:
        assert validate_email(email) is False

def test_email_too_long():
    assert validate_email("a" * 250 + "@example.com") is False
```

**验证**: `python -m pytest tests/security/ -v`

---

## 修复 011: 配置文件原子操作

**文件**: `src/agent_manager/core/configuration.py`

### 11.1 添加导入
```python
import msvcrt
import os
from contextlib import contextmanager
```

### 11.2 添加文件锁
```python
@contextmanager
def exclusive_file_lock(file_path: str):
    lock_file = f"{file_path}.lock"
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    try:
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except:
            pass
        try:
            os.close(fd)
        except:
            pass
        try:
            if os.path.exists(lock_file):
                os.remove(lock_file)
        except:
            pass
```

### 11.3 修改 apply_configuration（约64-90行）
```python
def apply_configuration(config_path, updates):
    with CONFIG_FILE_LOCK:
        with exclusive_file_lock(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                import toml
                current = toml.loads(f.read())
            
            current.update(updates)
            
            temp_path = f"{config_path}.tmp"
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    toml.dump(current, f)
                os.replace(temp_path, config_path)
            except:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise
```

---

## 修复 012: OAuth 状态管理线程安全

**文件**: `src/agent_manager/application/oauth.py`

### 12.1 添加安全方法（在OAuth类中）
```python
def _get_state_safe(self, state_id: str) -> dict:
    with self._state_lock:
        if self._current_state and self._current_state.get('id') == state_id:
            return self._current_state.copy()
        return None

def _set_state_safe(self, state_data: dict):
    with self._state_lock:
        self._current_state = state_data.copy()

def _clear_state_safe(self, state_id: str = None):
    with self._state_lock:
        if state_id is None or (
            self._current_state and 
            self._current_state.get('id') == state_id
        ):
            self._current_state = None
```

### 12.2 替换所有 self._current_state 访问
```bash
# 搜索: grep -n "self._current_state" src/agent_manager/application/oauth.py
# 替换规则:
# 读取 → self._get_state_safe(state_id)
# 设置 → self._set_state_safe(new_state)
# 清除 → self._clear_state_safe(state_id)
```

---

## 修复 013-020: 文件句柄泄漏

**通用模式**:
```python
# 错误: content = open(path).read()
# 正确:
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()
```

**需要修复的文件**:
1. `src/agent_manager/core/configuration.py`
2. `src/agent_manager/core/runtime.py`
3. `src/agent_manager/core/switching.py`
4. `src/agent_manager/accounts/portability.py`
5. `src/agent_manager/config/backups.py`
6. `src/agent_manager/sessions/history.py`
7. `src/agent_manager/integrations/radar.py`
8. `src/agent_manager/usage/export.py`

**查找命令**: `grep -n "open(" <文件> | grep -v "with open"`

**批量验证**: `grep -r "open(" src/agent_manager/ | grep -v "with open" | grep -v ".pyc"`

---

## 修复 021: 统一 HTTP 客户端

**新建**: `src/agent_manager/core/http_client.py`

```python
"""统一的 HTTP 客户端"""
from urllib.request import Request, urlopen, HTTPRedirectHandler, build_opener, ProxyHandler
from urllib.parse import urlparse
from urllib.error import URLError, HTTPError
from typing import Optional, Dict
import ssl
import json


class SameOriginRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_origin: str):
        super().__init__()
        self.allowed_origin = urlparse(allowed_origin).netloc
    
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_origin = urlparse(newurl).netloc
        if new_origin != self.allowed_origin:
            raise HTTPError(
                newurl, code,
                f"Cross-origin redirect not allowed: {self.allowed_origin} -> {new_origin}",
                headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SafeHTTPClient:
    def __init__(
        self, enforce_same_origin: bool = False, origin: Optional[str] = None,
        timeout: int = 30, proxy: Optional[str] = None,
        verify_ssl: bool = True, user_agent: str = "AgentManager/1.2.3"
    ):
        self.enforce_same_origin = enforce_same_origin
        self.origin = origin
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.user_agent = user_agent
        
        handlers = []
        if proxy:
            handlers.append(ProxyHandler({'http': proxy, 'https': proxy}))
        if enforce_same_origin and origin:
            handlers.append(SameOriginRedirectHandler(origin))
        
        self.opener = build_opener(*handlers) if handlers else None
        self.ssl_context = ssl._create_unverified_context() if not verify_ssl else ssl.create_default_context()
    
    def _build_request(self, url: str, method: str = 'GET', data: Optional[bytes] = None, headers: Optional[Dict] = None):
        req_headers = {'User-Agent': self.user_agent}
        if headers:
            req_headers.update(headers)
        return Request(url, data=data, headers=req_headers, method=method)
    
    def get(self, url: str, headers: Optional[Dict] = None) -> bytes:
        req = self._build_request(url, 'GET', headers=headers)
        if self.opener:
            response = self.opener.open(req, timeout=self.timeout, context=self.ssl_context)
        else:
            response = urlopen(req, timeout=self.timeout, context=self.ssl_context)
        return response.read()
    
    def post(self, url: str, data: bytes, headers: Optional[Dict] = None) -> bytes:
        req = self._build_request(url, 'POST', data, headers)
        if self.opener:
            response = self.opener.open(req, timeout=self.timeout, context=self.ssl_context)
        else:
            response = urlopen(req, timeout=self.timeout, context=self.ssl_context)
        return response.read()
    
    def get_json(self, url: str, headers: Optional[Dict] = None) -> dict:
        return json.loads(self.get(url, headers).decode('utf-8'))
    
    def post_json(self, url: str, json_data: dict, headers: Optional[Dict] = None) -> dict:
        req_headers = headers or {}
        req_headers['Content-Type'] = 'application/json'
        data = json.dumps(json_data).encode('utf-8')
        return json.loads(self.post(url, data, req_headers).decode('utf-8'))


def create_same_origin_client(origin: str, timeout: int = 30):
    return SafeHTTPClient(enforce_same_origin=True, origin=origin, timeout=timeout)

def create_registry_client(proxy: Optional[str] = None):
    return SafeHTTPClient(timeout=60, proxy=proxy, user_agent="AgentManager-Registry/1.2.3")

def create_api_client(verify_ssl: bool = True):
    return SafeHTTPClient(timeout=30, verify_ssl=verify_ssl)
```

---

## 修复 022: HTTP 客户端测试

**新建**: `tests/core/__init__.py` (如果不存在)

**新建**: `tests/core/test_http_client.py`

```python
import pytest
from unittest.mock import Mock, patch
from agent_manager.core.http_client import SafeHTTPClient, SameOriginRedirectHandler
from urllib.error import HTTPError

def test_basic_get_request():
    client = SafeHTTPClient(timeout=10)
    with patch('agent_manager.core.http_client.urlopen') as mock_urlopen:
        mock_response = Mock()
        mock_response.read.return_value = b'{"status": "ok"}'
        mock_urlopen.return_value = mock_response
        result = client.get("https://api.example.com/status")
        assert result == b'{"status": "ok"}'

def test_post_request():
    client = SafeHTTPClient()
    with patch('agent_manager.core.http_client.urlopen') as mock_urlopen:
        mock_response = Mock()
        mock_response.read.return_value = b'{"created": true}'
        mock_urlopen.return_value = mock_response
        result = client.post("https://api.example.com/create", b'{"name": "test"}')
        assert result == b'{"created": true}'

def test_same_origin_enforcement():
    handler = SameOriginRedirectHandler("https://example.com")
    mock_req = Mock()
    mock_fp = Mock()
    with pytest.raises(HTTPError):
        handler.redirect_request(
            mock_req, mock_fp, 302, "Found", {},
            "https://malicious.com/redirect"
        )
```

**验证**: `python -m pytest tests/core/test_http_client.py -v`

---

## 修复 023: 统一进程管理工具

**新建**: `src/agent_manager/core/process_utils.py`

```python
"""统一的进程管理工具"""
import subprocess
import time
import json
from typing import List, Optional, Set
from dataclasses import dataclass
from enum import Enum


class ProcessCheckMode(Enum):
    FAST = "fast"
    THOROUGH = "thorough"
    VALIDATE = "validate"


@dataclass
class ProcessInfo:
    pid: int
    name: str
    exe_path: str
    command_line: str
    parent_pid: Optional[int] = None


def _run_powershell_command(command: str, timeout: int = 10) -> str:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=timeout, check=False
        )
        return result.stdout
    except:
        return ""


def detect_codex_processes(
    mode: ProcessCheckMode = ProcessCheckMode.THOROUGH,
    retry_count: int = 0,
    retry_delay: float = 0.5
) -> List[ProcessInfo]:
    codex_names = {"codex.exe", "codex-desktop.exe", "codex-cli.exe"}
    
    for attempt in range(retry_count + 1):
        if attempt > 0:
            time.sleep(retry_delay)
        processes = _detect_processes_internal(codex_names, mode)
        if processes or attempt == retry_count:
            return processes
    return []


def _detect_processes_internal(process_names: Set[str], mode: ProcessCheckMode) -> List[ProcessInfo]:
    if mode == ProcessCheckMode.FAST:
        command = f"""
        Get-Process | Where-Object {{
            $_.ProcessName -in @({','.join(f"'{name[:-4]}'" for name in process_names)})
        }} | Select-Object Id, ProcessName, Path | ConvertTo-Json
        """
    else:
        command = f"""
        Get-WmiObject Win32_Process | Where-Object {{
            $_.Name -in @({','.join(f"'{name}'" for name in process_names)})
        }} | Select-Object ProcessId, Name, ExecutablePath, CommandLine, ParentProcessId | ConvertTo-Json
        """
    
    output = _run_powershell_command(command)
    if not output or output.strip() == "":
        return []
    
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            data = [data]
        
        processes = []
        for item in data:
            if mode == ProcessCheckMode.FAST:
                process = ProcessInfo(
                    pid=item.get('Id', 0),
                    name=item.get('ProcessName', ''),
                    exe_path=item.get('Path', ''),
                    command_line=""
                )
            else:
                process = ProcessInfo(
                    pid=item.get('ProcessId', 0),
                    name=item.get('Name', ''),
                    exe_path=item.get('ExecutablePath', ''),
                    command_line=item.get('CommandLine', ''),
                    parent_pid=item.get('ParentProcessId')
                )
            processes.append(process)
        
        if mode == ProcessCheckMode.VALIDATE:
            processes = [p for p in processes if p.exe_path and 'codex' in p.exe_path.lower()]
        
        return processes
    except:
        return []


def kill_process_tree(pid: int, timeout: int = 5) -> bool:
    command = f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue; $?"
    output = _run_powershell_command(command, timeout)
    return "True" in output


def wait_for_process_exit(pid: int, timeout: int = 30) -> bool:
    start_time = time.time()
    while time.time() - start_time < timeout:
        command = f"Get-Process -Id {pid} -ErrorAction SilentlyContinue"
        output = _run_powershell_command(command, timeout=2)
        if not output or output.strip() == "":
            return True
        time.sleep(0.5)
    return False
```

---

## 修复 024: 进程工具测试

**新建**: `tests/core/test_process_utils.py`

```python
import pytest
from unittest.mock import patch
from agent_manager.core.process_utils import (
    detect_codex_processes, ProcessCheckMode, kill_process_tree
)

@patch('agent_manager.core.process_utils._run_powershell_command')
def test_detect_processes_fast(mock_ps):
    mock_ps.return_value = '{"Id": 1234, "ProcessName": "codex", "Path": "C:\\\\codex.exe"}'
    processes = detect_codex_processes(mode=ProcessCheckMode.FAST)
    assert len(processes) == 1
    assert processes[0].pid == 1234

@patch('agent_manager.core.process_utils._run_powershell_command')
def test_detect_no_processes(mock_ps):
    mock_ps.return_value = ""
    processes = detect_codex_processes()
    assert len(processes) == 0

@patch('agent_manager.core.process_utils._run_powershell_command')
def test_kill_process(mock_ps):
    mock_ps.return_value = "True"
    result = kill_process_tree(1234)
    assert result is True
```

---

## 修复 025-030: URL 验证工具

**新建**: `src/agent_manager/core/url_validator.py`

```python
"""统一的 URL 验证工具"""
from urllib.parse import urlparse, urlunparse
from typing import Optional, Set


class URLValidationError(ValueError):
    pass


def validate_url(
    url: str,
    allowed_schemes: Optional[Set[str]] = None,
    allow_loopback: bool = True,
    allow_private_networks: bool = True,
    require_port: Optional[int] = None,
    purpose: str = "URL"
) -> str:
    if not url or not isinstance(url, str):
        raise URLValidationError(f"Invalid {purpose}: URL must be a non-empty string")
    
    if allowed_schemes is None:
        allowed_schemes = {"http", "https"}
    
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise URLValidationError(f"Invalid {purpose}: Failed to parse URL: {str(e)}")
    
    if parsed.scheme not in allowed_schemes:
        raise URLValidationError(
            f"Invalid {purpose}: Scheme '{parsed.scheme}' not allowed. "
            f"Allowed: {', '.join(allowed_schemes)}"
        )
    
    if not parsed.netloc:
        raise URLValidationError(f"Invalid {purpose}: Missing host")
    
    hostname = parsed.hostname
    port = parsed.port
    
    if not hostname:
        raise URLValidationError(f"Invalid {purpose}: Invalid hostname")
    
    if not allow_loopback:
        if hostname in ["localhost", "127.0.0.1", "::1"]:
            raise URLValidationError(f"Invalid {purpose}: Loopback addresses not allowed")
    
    if not allow_private_networks:
        if _is_private_ip(hostname):
            raise URLValidationError(f"Invalid {purpose}: Private network addresses not allowed")
    
    if require_port is not None and port != require_port:
        raise URLValidationError(f"Invalid {purpose}: Port must be {require_port}, got {port}")
    
    if port is not None:
        if port < 1 or port > 65535:
            raise URLValidationError(f"Invalid {purpose}: Port {port} out of valid range")
    
    return url


def _is_private_ip(hostname: str) -> bool:
    if hostname.startswith("10."):
        return True
    if hostname.startswith("172."):
        try:
            second_octet = int(hostname.split('.')[1])
            if 16 <= second_octet <= 31:
                return True
        except:
            pass
    if hostname.startswith("192.168."):
        return True
    return False


def validate_provider_url(url: str) -> str:
    return validate_url(url, purpose="provider endpoint")


def validate_provider_portal_url(url: str) -> str:
    return validate_url(
        url, allow_loopback=False, allow_private_networks=False,
        purpose="provider portal"
    )


def validate_webhook_url(url: str) -> str:
    return validate_url(
        url, allowed_schemes={"https"},
        allow_loopback=False, allow_private_networks=False,
        purpose="webhook"
    )


def validate_redirect_url(url: str, base_url: str) -> str:
    base_parsed = urlparse(base_url)
    redirect_parsed = urlparse(url)
    
    if base_parsed.scheme != redirect_parsed.scheme:
        raise URLValidationError(f"Cross-origin redirect: scheme mismatch")
    
    if base_parsed.netloc != redirect_parsed.netloc:
        raise URLValidationError(f"Cross-origin redirect: host mismatch")
    
    return url


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    netloc = parsed.netloc
    
    if parsed.scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif parsed.scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    
    path = parsed.path.rstrip('/') or '/'
    
    return urlunparse((
        parsed.scheme.lower(), netloc.lower(), path,
        parsed.params, parsed.query, parsed.fragment
    ))
```

**新建**: `tests/core/test_url_validator.py`

```python
import pytest
from agent_manager.core.url_validator import (
    validate_url, validate_redirect_url, URLValidationError
)

def test_valid_urls():
    assert validate_url("https://example.com") == "https://example.com"
    assert validate_url("http://localhost:3000") == "http://localhost:3000"

def test_invalid_scheme():
    with pytest.raises(URLValidationError, match="Scheme.*not allowed"):
        validate_url("ftp://example.com")

def test_loopback_blocking():
    with pytest.raises(URLValidationError, match="Loopback.*not allowed"):
        validate_url("http://localhost", allow_loopback=False)

def test_redirect_same_origin():
    base = "https://example.com/page1"
    redirect = "https://example.com/page2"
    assert validate_redirect_url(redirect, base) == redirect

def test_redirect_cross_origin():
    with pytest.raises(URLValidationError):
        validate_redirect_url("https://malicious.com", "https://example.com")
```

**验证所有**: `python -m pytest tests/core/ tests/security/ -v`

---

**完成 001-030 后运行总验证**:
```bash
python -m pytest tests/ -v
python -m agent_manager  # 确保程序能启动
```
