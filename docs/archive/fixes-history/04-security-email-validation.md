# 修复 5: 邮箱格式验证

## 文件: `src/agent_manager/accounts/reauthentication.py`

**位置**: Line 24 附近

### 步骤 1: 在文件顶部添加导入和正则表达式

```python
import re

# 邮箱验证正则表达式
EMAIL_PATTERN = re.compile(
    r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
)
```

### 步骤 2: 创建验证函数

在文件中任意位置添加：

```python
def validate_email(email: str) -> bool:
    """
    验证邮箱格式
    
    Args:
        email: 邮箱地址字符串
    
    Returns:
        True 如果格式有效
    """
    if not email or not isinstance(email, str):
        return False
    
    if len(email) > 254:  # RFC 5321 限制
        return False
    
    return EMAIL_PATTERN.match(email) is not None
```

### 步骤 3: 在使用邮箱的地方添加验证

查找邮箱比较代码（类似 `if account_email.lower() == snapshot_email.lower():`）

**修改前**:
```python
if account_email.lower() == snapshot_email.lower():
    # 邮箱匹配逻辑
```

**修改后**:
```python
# 验证邮箱格式
if not validate_email(account_email):
    raise ValueError(f"Invalid email format: {account_email}")

if not validate_email(snapshot_email):
    raise ValueError(f"Invalid email format in snapshot: {snapshot_email}")

if account_email.lower() == snapshot_email.lower():
    # 邮箱匹配逻辑
```

**验证命令**:
```bash
python -m pytest tests/accounts/test_oauth_reauthentication.py -v
```
