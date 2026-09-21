# 修复 4: DPAPI 输入验证

## 文件: `src/agent_manager/core/credential_store.py`

**位置**: Line 36 附近，查找 `dpapi_unprotect` 和 `dpapi_protect` 函数

### 修改 dpapi_unprotect 函数

**替换前的代码**:
```python
def dpapi_unprotect(ciphertext: bytes) -> bytes:
    """使用 DPAPI 解密数据"""
    return win32crypt.CryptUnprotectData(ciphertext, None, None, None, 0)[1]
```

**替换为**:
```python
def dpapi_unprotect(ciphertext: bytes) -> bytes:
    """
    使用 DPAPI 解密数据
    
    Args:
        ciphertext: 加密的字节数据
    
    Returns:
        解密后的字节数据
    
    Raises:
        ValueError: 输入数据无效
    """
    # 输入验证
    if not isinstance(ciphertext, bytes):
        raise ValueError("Ciphertext must be bytes")
    
    if not ciphertext:
        raise ValueError("Ciphertext cannot be empty")
    
    # DPAPI 加密数据至少需要16字节（头部信息）
    if len(ciphertext) < 16:
        raise ValueError("Ciphertext too short: minimum 16 bytes required")
    
    # 防止过大的数据（1MB限制）
    MAX_CIPHERTEXT_SIZE = 1024 * 1024
    if len(ciphertext) > MAX_CIPHERTEXT_SIZE:
        raise ValueError(f"Ciphertext too large: maximum {MAX_CIPHERTEXT_SIZE} bytes allowed")
    
    try:
        # 解密数据
        plaintext = win32crypt.CryptUnprotectData(ciphertext, None, None, None, 0)[1]
        return plaintext
    except Exception as e:
        raise ValueError(f"DPAPI decryption failed: {str(e)}")
```

### 同样修改 dpapi_protect 函数

如果存在 `dpapi_protect` 函数，也添加验证：

```python
def dpapi_protect(plaintext: bytes) -> bytes:
    """
    使用 DPAPI 加密数据（添加输入验证）
    
    Args:
        plaintext: 明文字节数据
    
    Returns:
        加密后的字节数据
    
    Raises:
        ValueError: 输入数据无效
    """
    # 输入验证
    if not isinstance(plaintext, bytes):
        raise ValueError("Plaintext must be bytes")
    
    if not plaintext:
        raise ValueError("Plaintext cannot be empty")
    
    # 防止过大的数据
    MAX_PLAINTEXT_SIZE = 1024 * 1024
    if len(plaintext) > MAX_PLAINTEXT_SIZE:
        raise ValueError(f"Plaintext too large: maximum {MAX_PLAINTEXT_SIZE} bytes allowed")
    
    try:
        ciphertext = win32crypt.CryptProtectData(plaintext, None, None, None, None, 0)
        return ciphertext
    except Exception as e:
        raise ValueError(f"DPAPI encryption failed: {str(e)}")
```

**验证命令**:
```bash
python -m pytest tests/accounts/test_relay_key_management.py -v
```
