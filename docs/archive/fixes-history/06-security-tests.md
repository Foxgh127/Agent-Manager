# 修复 7-10: 安全测试套件

## 创建测试目录

```bash
mkdir -p tests/security
```

---

## 文件 1: `tests/security/__init__.py`

```python
"""安全测试模块"""
```

---

## 文件 2: `tests/security/test_rate_limiting.py`

```python
"""速率限制器测试"""
import pytest
import time
from agent_manager.core.rate_limiter import RateLimiter, IPRateLimiter


def test_rate_limiter_basic():
    """测试基本速率限制功能"""
    limiter = RateLimiter(max_requests=3, window_seconds=1)
    
    # 前3个请求应该成功
    assert limiter.is_allowed("client1") is True
    assert limiter.is_allowed("client1") is True
    assert limiter.is_allowed("client1") is True
    
    # 第4个请求应该被拒绝
    assert limiter.is_allowed("client1") is False
    
    # 等待窗口过期
    time.sleep(1.1)
    
    # 应该可以再次请求
    assert limiter.is_allowed("client1") is True


def test_rate_limiter_multiple_clients():
    """测试多客户端隔离"""
    limiter = RateLimiter(max_requests=2, window_seconds=1)
    
    # 客户端1的请求
    assert limiter.is_allowed("client1") is True
    assert limiter.is_allowed("client1") is True
    assert limiter.is_allowed("client1") is False
    
    # 客户端2不应该受影响
    assert limiter.is_allowed("client2") is True
    assert limiter.is_allowed("client2") is True
    assert limiter.is_allowed("client2") is False


def test_rate_limiter_remaining():
    """测试剩余请求数查询"""
    limiter = RateLimiter(max_requests=5, window_seconds=10)
    
    assert limiter.get_remaining("client1") == 5
    
    limiter.is_allowed("client1")
    assert limiter.get_remaining("client1") == 4
    
    limiter.is_allowed("client1")
    limiter.is_allowed("client1")
    assert limiter.get_remaining("client1") == 2


def test_rate_limiter_reset():
    """测试重置功能"""
    limiter = RateLimiter(max_requests=2, window_seconds=10)
    
    limiter.is_allowed("client1")
    limiter.is_allowed("client1")
    assert limiter.is_allowed("client1") is False
    
    limiter.reset("client1")
    assert limiter.is_allowed("client1") is True


def test_ip_rate_limiter_extract():
    """测试IP提取功能"""
    limiter = IPRateLimiter()
    
    # 测试 X-Forwarded-For
    headers = {'X-Forwarded-For': '192.168.1.100, 10.0.0.1'}
    assert limiter.extract_client_id(headers) == '192.168.1.100'
    
    # 测试 X-Real-IP
    headers = {'X-Real-IP': '192.168.1.200'}
    assert limiter.extract_client_id(headers) == '192.168.1.200'
    
    # 测试 Remote-Addr
    headers = {'Remote-Addr': '192.168.1.300'}
    assert limiter.extract_client_id(headers) == '192.168.1.300'
    
    # 测试优先级：X-Forwarded-For > X-Real-IP
    headers = {
        'X-Forwarded-For': '192.168.1.100',
        'X-Real-IP': '192.168.1.200'
    }
    assert limiter.extract_client_id(headers) == '192.168.1.100'
```

---

## 文件 3: `tests/security/test_jwt_validation.py`

```python
"""JWT 签名验证测试"""
import pytest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from agent_manager.core.auth import (
    validate_jwt_with_signature,
    load_jwt_public_key,
    parse_jwt_claims
)


@pytest.fixture
def rsa_key_pair():
    """生成RSA密钥对用于测试"""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
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
    """测试有效的JWT签名验证"""
    private_pem, public_pem = rsa_key_pair
    
    load_jwt_public_key('test-key', public_pem)
    
    payload = {'sub': 'user123', 'exp': 9999999999}
    token = jwt.encode(payload, private_pem, algorithm='RS256', headers={'kid': 'test-key'})
    
    result = validate_jwt_with_signature(token, key_id='test-key')
    assert result['sub'] == 'user123'


def test_jwt_invalid_signature(rsa_key_pair):
    """测试无效的JWT签名被拒绝"""
    private_pem, public_pem = rsa_key_pair
    
    wrong_private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    wrong_private_pem = wrong_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    
    load_jwt_public_key('test-key', public_pem)
    
    payload = {'sub': 'user123', 'exp': 9999999999}
    token = jwt.encode(payload, wrong_private_pem, algorithm='RS256', headers={'kid': 'test-key'})
    
    with pytest.raises(ValueError, match="signature verification failed"):
        validate_jwt_with_signature(token, key_id='test-key')


def test_jwt_expired_token(rsa_key_pair):
    """测试过期的JWT被拒绝"""
    private_pem, public_pem = rsa_key_pair
    
    load_jwt_public_key('test-key', public_pem)
    
    payload = {'sub': 'user123', 'exp': 1}
    token = jwt.encode(payload, private_pem, algorithm='RS256', headers={'kid': 'test-key'})
    
    with pytest.raises(ValueError, match="expired"):
        validate_jwt_with_signature(token, key_id='test-key')


def test_jwt_malformed_token():
    """测试格式错误的JWT"""
    with pytest.raises(ValueError, match="decode error"):
        validate_jwt_with_signature("not.a.jwt")
```

---

## 文件 4: `tests/security/test_dpapi_validation.py`

```python
"""DPAPI 输入验证测试"""
import pytest
import sys
from agent_manager.core.credential_store import dpapi_protect, dpapi_unprotect


def test_dpapi_empty_input():
    """测试空输入被拒绝"""
    with pytest.raises(ValueError, match="cannot be empty"):
        dpapi_protect(b"")
    
    with pytest.raises(ValueError, match="cannot be empty"):
        dpapi_unprotect(b"")


def test_dpapi_invalid_type():
    """测试非字节类型被拒绝"""
    with pytest.raises(ValueError, match="must be bytes"):
        dpapi_protect("not bytes")
    
    with pytest.raises(ValueError, match="must be bytes"):
        dpapi_unprotect("not bytes")


def test_dpapi_too_short_ciphertext():
    """测试过短的密文被拒绝"""
    short_data = b"short"
    with pytest.raises(ValueError, match="too short"):
        dpapi_unprotect(short_data)


def test_dpapi_too_large_input():
    """测试过大的输入被拒绝"""
    large_data = b"x" * (1024 * 1024 + 1)
    
    with pytest.raises(ValueError, match="too large"):
        dpapi_protect(large_data)


@pytest.mark.skipif(
    not sys.platform.startswith('win'),
    reason="DPAPI only available on Windows"
)
def test_dpapi_valid_roundtrip():
    """测试有效的加密解密循环（仅Windows）"""
    plaintext = b"secret data"
    
    ciphertext = dpapi_protect(plaintext)
    assert len(ciphertext) >= 16
    
    decrypted = dpapi_unprotect(ciphertext)
    assert decrypted == plaintext
```

---

## 文件 5: `tests/security/test_email_validation.py`

```python
"""邮箱格式验证测试"""
import pytest
from agent_manager.accounts.reauthentication import validate_email


def test_valid_emails():
    """测试有效的邮箱格式"""
    valid_emails = [
        "user@example.com",
        "test.user@example.com",
        "user+tag@example.co.uk",
        "user_name@example-domain.com",
        "123@example.com",
        "a@b.co",
    ]
    
    for email in valid_emails:
        assert validate_email(email) is True, f"Should accept: {email}"


def test_invalid_emails():
    """测试无效的邮箱格式"""
    invalid_emails = [
        "",
        "notanemail",
        "@example.com",
        "user@",
        "user @example.com",
        "user@example",
        "user..name@example.com",
        "user@.com",
        ".user@example.com",
        "user.@example.com",
        "user@exam ple.com",
    ]
    
    for email in invalid_emails:
        assert validate_email(email) is False, f"Should reject: {email}"


def test_email_too_long():
    """测试过长的邮箱地址"""
    long_email = "a" * 250 + "@example.com"
    assert validate_email(long_email) is False


def test_email_none_or_non_string():
    """测试None和非字符串输入"""
    assert validate_email(None) is False
    assert validate_email(123) is False
    assert validate_email([]) is False
```

---

**验证所有安全测试**:
```bash
python -m pytest tests/security/ -v
```
