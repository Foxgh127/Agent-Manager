# 修复 21-22: 统一 HTTP 客户端

## 文件 1: `src/agent_manager/core/http_client.py` (新建)

```python
"""统一的 HTTP 客户端 - 替代分散的 urllib 使用"""
from urllib.request import Request, urlopen, HTTPRedirectHandler, build_opener, ProxyHandler
from urllib.parse import urlparse
from urllib.error import URLError, HTTPError
from typing import Optional, Dict
import ssl
import json


class SameOriginRedirectHandler(HTTPRedirectHandler):
    """同源重定向处理器 - 仅允许同域名重定向"""
    
    def __init__(self, allowed_origin: str):
        super().__init__()
        self.allowed_origin = urlparse(allowed_origin).netloc
    
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """检查重定向目标是否同源"""
        new_origin = urlparse(newurl).netloc
        
        if new_origin != self.allowed_origin:
            raise HTTPError(
                newurl, code,
                f"Cross-origin redirect not allowed: {self.allowed_origin} -> {new_origin}",
                headers, fp
            )
        
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SafeHTTPClient:
    """
    安全的 HTTP 客户端
    
    特性:
    - 可选的同源策略
    - 代理支持
    - 超时控制
    - 自定义头部
    - SSL 验证
    """
    
    def __init__(
        self,
        enforce_same_origin: bool = False,
        origin: Optional[str] = None,
        timeout: int = 30,
        proxy: Optional[str] = None,
        verify_ssl: bool = True,
        user_agent: str = "AgentManager/1.2.3"
    ):
        self.enforce_same_origin = enforce_same_origin
        self.origin = origin
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.user_agent = user_agent
        
        # 构建 opener
        handlers = []
        
        if proxy:
            handlers.append(ProxyHandler({'http': proxy, 'https': proxy}))
        
        if enforce_same_origin and origin:
            handlers.append(SameOriginRedirectHandler(origin))
        
        self.opener = build_opener(*handlers) if handlers else None
        
        if not verify_ssl:
            self.ssl_context = ssl._create_unverified_context()
        else:
            self.ssl_context = ssl.create_default_context()
    
    def _build_request(
        self,
        url: str,
        method: str = 'GET',
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None
    ) -> Request:
        """构建请求对象"""
        req_headers = {'User-Agent': self.user_agent}
        
        if headers:
            req_headers.update(headers)
        
        return Request(url, data=data, headers=req_headers, method=method)
    
    def get(self, url: str, headers: Optional[Dict[str, str]] = None) -> bytes:
        """发送 GET 请求"""
        req = self._build_request(url, method='GET', headers=headers)
        
        if self.opener:
            response = self.opener.open(req, timeout=self.timeout, context=self.ssl_context)
        else:
            response = urlopen(req, timeout=self.timeout, context=self.ssl_context)
        
        return response.read()
    
    def post(self, url: str, data: bytes, headers: Optional[Dict[str, str]] = None) -> bytes:
        """发送 POST 请求"""
        req = self._build_request(url, method='POST', data=data, headers=headers)
        
        if self.opener:
            response = self.opener.open(req, timeout=self.timeout, context=self.ssl_context)
        else:
            response = urlopen(req, timeout=self.timeout, context=self.ssl_context)
        
        return response.read()
    
    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None) -> dict:
        """GET 请求并解析 JSON"""
        response_data = self.get(url, headers)
        return json.loads(response_data.decode('utf-8'))
    
    def post_json(self, url: str, json_data: dict, headers: Optional[Dict[str, str]] = None) -> dict:
        """POST JSON 数据并解析响应"""
        req_headers = headers or {}
        req_headers['Content-Type'] = 'application/json'
        
        data = json.dumps(json_data).encode('utf-8')
        response_data = self.post(url, data, req_headers)
        
        return json.loads(response_data.decode('utf-8'))


def create_same_origin_client(origin: str, timeout: int = 30) -> SafeHTTPClient:
    """创建强制同源的客户端"""
    return SafeHTTPClient(
        enforce_same_origin=True,
        origin=origin,
        timeout=timeout
    )


def create_registry_client(proxy: Optional[str] = None) -> SafeHTTPClient:
    """创建访问 npm registry 的客户端"""
    return SafeHTTPClient(
        timeout=60,
        proxy=proxy,
        user_agent="AgentManager-Registry/1.2.3"
    )


def create_api_client(verify_ssl: bool = True) -> SafeHTTPClient:
    """创建API调用客户端"""
    return SafeHTTPClient(
        timeout=30,
        verify_ssl=verify_ssl
    )
```

**验证**:
```bash
python -c "from agent_manager.core.http_client import SafeHTTPClient; print('OK')"
```

---

## 文件 2: `tests/core/test_http_client.py` (新建)

```python
"""HTTP 客户端测试"""
import pytest
from unittest.mock import Mock, patch
from agent_manager.core.http_client import (
    SafeHTTPClient,
    SameOriginRedirectHandler,
    create_same_origin_client
)
from urllib.error import HTTPError


def test_basic_get_request():
    """测试基本 GET 请求"""
    client = SafeHTTPClient(timeout=10)
    
    with patch('agent_manager.core.http_client.urlopen') as mock_urlopen:
        mock_response = Mock()
        mock_response.read.return_value = b'{"status": "ok"}'
        mock_urlopen.return_value = mock_response
        
        result = client.get("https://api.example.com/status")
        assert result == b'{"status": "ok"}'


def test_post_request():
    """测试 POST 请求"""
    client = SafeHTTPClient()
    
    with patch('agent_manager.core.http_client.urlopen') as mock_urlopen:
        mock_response = Mock()
        mock_response.read.return_value = b'{"created": true}'
        mock_urlopen.return_value = mock_response
        
        result = client.post(
            "https://api.example.com/create",
            data=b'{"name": "test"}'
        )
        assert result == b'{"created": true}'


def test_get_json_convenience():
    """测试 JSON 便捷方法"""
    client = SafeHTTPClient()
    
    with patch('agent_manager.core.http_client.urlopen') as mock_urlopen:
        mock_response = Mock()
        mock_response.read.return_value = b'{"key": "value"}'
        mock_urlopen.return_value = mock_response
        
        result = client.get_json("https://api.example.com/data")
        assert result == {"key": "value"}


def test_same_origin_enforcement():
    """测试同源策略"""
    handler = SameOriginRedirectHandler("https://example.com")
    
    mock_req = Mock()
    mock_fp = Mock()
    
    with pytest.raises(HTTPError):
        handler.redirect_request(
            mock_req, mock_fp, 302, "Found",
            {}, "https://malicious.com/redirect"
        )


def test_custom_headers():
    """测试自定义请求头"""
    client = SafeHTTPClient(user_agent="CustomAgent/1.0")
    
    with patch('agent_manager.core.http_client.urlopen') as mock_urlopen:
        mock_response = Mock()
        mock_response.read.return_value = b'ok'
        mock_urlopen.return_value = mock_response
        
        client.get("https://api.example.com", headers={"X-Custom": "value"})
        
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert request.get_header('User-agent') == "CustomAgent/1.0"
```

**验证**:
```bash
python -m pytest tests/core/test_http_client.py -v
```
