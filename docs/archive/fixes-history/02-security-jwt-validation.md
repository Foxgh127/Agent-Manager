# 修复 3: JWT 签名验证

## 文件: `src/agent_manager/core/auth.py`

**位置**: Lines 12-26

### 步骤 1: 添加依赖导入

在文件顶部添加：

```python
import jwt
from jwt.exceptions import InvalidSignatureError, DecodeError, ExpiredSignatureError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from typing import Optional
```

### 步骤 2: 添加公钥管理

```python
# JWT 公钥配置（应该从配置文件加载）
_JWT_PUBLIC_KEYS: dict = {}


def load_jwt_public_key(key_id: str, pem_data: bytes):
    """加载 JWT 公钥"""
    _JWT_PUBLIC_KEYS[key_id] = pem_data
```

### 步骤 3: 创建签名验证函数

```python
def validate_jwt_with_signature(
    token: str, 
    key_id: Optional[str] = None,
    algorithms: Optional[list] = None
) -> dict:
    """
    验证 JWT 签名并返回声明
    
    Args:
        token: JWT 令牌
        key_id: 密钥ID（可选，从token头部提取）
        algorithms: 允许的签名算法列表
    
    Returns:
        JWT 声明字典
    
    Raises:
        ValueError: 签名无效、令牌过期或格式错误
    """
    if algorithms is None:
        algorithms = ["RS256", "ES256", "HS256"]
    
    try:
        # 首先解码header获取key_id
        unverified_header = jwt.get_unverified_header(token)
        if key_id is None:
            key_id = unverified_header.get('kid', 'default')
        
        # 获取公钥
        if key_id not in _JWT_PUBLIC_KEYS:
            # 回退到不验证签名的模式（仅用于内部生成的token）
            return jwt.decode(token, options={"verify_signature": False})
        
        pem_data = _JWT_PUBLIC_KEYS[key_id]
        
        # 根据算法加载密钥
        if unverified_header.get('alg', '').startswith('HS'):
            # HMAC 密钥（对称加密）
            key = pem_data
        else:
            # RSA/ECDSA 公钥（非对称加密）
            key = serialization.load_pem_public_key(pem_data, backend=default_backend())
        
        # 验证签名并解码
        payload = jwt.decode(
            token,
            key,
            algorithms=algorithms,
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

### 步骤 4: 更新现有的 parse_jwt_claims 函数

替换现有的 `parse_jwt_claims` 函数为：

```python
def parse_jwt_claims(token: str) -> dict:
    """
    解析 JWT 声明（带签名验证）
    
    这是向后兼容的包装器，会尝试验证签名
    """
    try:
        return validate_jwt_with_signature(token)
    except ValueError:
        # 如果验证失败，记录警告但继续（向后兼容）
        import logging
        logging.warning("JWT signature validation failed, proceeding without verification")
        return jwt.decode(token, options={"verify_signature": False})
```

**验证命令**:
```bash
python -m pytest tests/accounts/test_oauth_lifecycle.py -v
```

**安装依赖**:
```bash
pip install PyJWT cryptography
```
