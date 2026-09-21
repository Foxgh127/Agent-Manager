# Code Style Guide

## Python 代码规范

### PEP 8 基础规范

遵循 [PEP 8](https://pep8.org/) Python 代码风格指南。

#### 缩进和空格

```python
# ✓ 正确
def function_name(param1, param2):
    if condition:
        result = do_something()
        return result
    
# ✗ 错误
def function_name(param1,param2):
  if condition:
      result=do_something()
      return result
```

#### 最大行长度

- 代码：100 字符
- 注释和文档：72 字符

```python
# ✓ 正确 - 使用括号换行
result = some_function_with_long_name(
    parameter1, parameter2,
    parameter3, parameter4
)

# ✗ 错误 - 超过100字符
result = some_function_with_long_name(parameter1, parameter2, parameter3, parameter4)
```

### 命名规范

```python
# 模块名：小写+下划线
# file: http_client.py

# 类名：大驼峰
class HTTPClient:
    pass

class URLValidator:
    pass

# 函数和变量：小写+下划线
def get_user_name():
    user_name = "John"
    return user_name

# 常量：大写+下划线
MAX_RETRY_COUNT = 3
DEFAULT_TIMEOUT = 30

# 私有成员：单下划线前缀
class MyClass:
    def _private_method(self):
        pass
    
    def __really_private(self):  # 名称改写
        pass
```

### 导入规范

```python
# 导入顺序：
# 1. 标准库
# 2. 第三方库
# 3. 本地模块

# ✓ 正确
from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import Optional, List

import requests
from flask import Flask

from agent_manager.core import utils
from agent_manager.core.http_client import SafeHTTPClient

# ✗ 错误 - 混乱的导入顺序
from agent_manager.core import utils
import sys
import requests
from typing import Optional
import os
```

### 类型提示

```python
from typing import Optional, List, Dict, Any

# ✓ 正确 - 完整类型提示
def process_data(
    items: List[str],
    config: Dict[str, Any],
    timeout: int = 30
) -> Optional[dict]:
    """处理数据并返回结果"""
    if not items:
        return None
    return {"count": len(items)}

# ✗ 错误 - 缺少类型提示
def process_data(items, config, timeout=30):
    if not items:
        return None
    return {"count": len(items)}
```

### 文档字符串

```python
def complex_function(param1: str, param2: int, param3: Optional[dict] = None) -> dict:
    """执行复杂操作
    
    这是一个详细的描述段落，解释函数的目的和行为。
    可以包含多行内容。
    
    Args:
        param1: 第一个参数的描述
        param2: 第二个参数的描述，应该是正整数
        param3: 可选的配置字典，默认为 None
        
    Returns:
        包含处理结果的字典，格式如下：
        {
            "status": "success" | "error",
            "data": Any,
            "message": str
        }
        
    Raises:
        ValueError: 当 param2 为负数时抛出
        TypeError: 当 param3 不是字典时抛出
        
    Examples:
        >>> result = complex_function("test", 42)
        >>> print(result["status"])
        success
    """
    if param2 < 0:
        raise ValueError("param2 must be positive")
    
    if param3 is not None and not isinstance(param3, dict):
        raise TypeError("param3 must be a dict")
    
    return {
        "status": "success",
        "data": f"{param1}:{param2}",
        "message": "OK"
    }
```

### 错误处理

```python
# ✓ 正确 - 具体的异常类型
try:
    result = process_data(items)
except ValueError as e:
    logger.error(f"Invalid data: {e}")
    raise
except ConnectionError as e:
    logger.warning(f"Connection failed: {e}")
    return None

# ✗ 错误 - 捕获所有异常
try:
    result = process_data(items)
except Exception:  # 太宽泛
    pass  # 忽略错误
```

### 上下文管理器

```python
# ✓ 正确 - 使用上下文管理器
with open("file.txt", "r") as f:
    content = f.read()

with client.open_stream(url) as response:
    process(response)

# ✗ 错误 - 手动管理资源
f = open("file.txt", "r")
content = f.read()
f.close()  # 可能不会执行
```

## JavaScript/React 规范

### 基础规范

```javascript
// ✓ 正确 - 使用 const/let
const MAX_COUNT = 100;
let counter = 0;

// ✗ 错误 - 使用 var
var counter = 0;
```

### 组件规范

```javascript
// ✓ 正确 - 函数组件
import React, { useState, useEffect } from 'react';

export function UserProfile({ userId }) {
  const [user, setUser] = useState(null);
  
  useEffect(() => {
    fetchUser(userId).then(setUser);
  }, [userId]);
  
  if (!user) return <div>Loading...</div>;
  
  return (
    <div className="user-profile">
      <h1>{user.name}</h1>
      <p>{user.email}</p>
    </div>
  );
}

// ✗ 错误 - 类组件（除非必要）
class UserProfile extends React.Component {
  // ...旧式代码
}
```

### 命名规范

```javascript
// 组件：大驼峰
function UserProfile() {}
class DataTable extends Component {}

// 函数和变量：小驼峰
const userName = "John";
function getUserData() {}

// 常量：大写+下划线
const MAX_RETRY_COUNT = 3;
const API_ENDPOINT = "https://api.example.com";

// 私有方法/变量：下划线前缀（约定）
function _internalHelper() {}
const _privateCache = {};
```

## 通用原则

### DRY (Don't Repeat Yourself)

```python
# ✓ 正确 - 提取公共逻辑
def validate_and_process(data, validator):
    if not validator(data):
        raise ValueError("Invalid data")
    return process(data)

result1 = validate_and_process(data1, email_validator)
result2 = validate_and_process(data2, url_validator)

# ✗ 错误 - 重复代码
if not email_validator(data1):
    raise ValueError("Invalid data")
result1 = process(data1)

if not url_validator(data2):
    raise ValueError("Invalid data")
result2 = process(data2)
```

### KISS (Keep It Simple, Stupid)

```python
# ✓ 正确 - 简单直接
def is_adult(age):
    return age >= 18

# ✗ 错误 - 过度复杂
def is_adult(age):
    if age >= 18:
        return True
    else:
        return False
```

### YAGNI (You Aren't Gonna Need It)

```python
# ✓ 正确 - 只实现需要的功能
class User:
    def __init__(self, name):
        self.name = name

# ✗ 错误 - 添加不需要的功能
class User:
    def __init__(self, name):
        self.name = name
        self.preferences = {}  # 还没人用到
        self.cache = {}  # 可能永远不需要
        self.history = []  # 预备将来用
```

## 注释规范

### 何时添加注释

```python
# ✓ 正确 - 解释为什么
# 使用 UTF-8 编码以支持中文路径
file = open(path, encoding="utf-8")

# 延迟 100ms 等待浏览器渲染完成
time.sleep(0.1)

# ✗ 错误 - 重复代码
# 打开文件
file = open(path)

# 设置 x 为 10
x = 10
```

### 何时不需要注释

```python
# ✓ 正确 - 代码自解释
def calculate_total_price(items, tax_rate):
    subtotal = sum(item.price for item in items)
    tax = subtotal * tax_rate
    return subtotal + tax

# ✗ 错误 - 不必要的注释
def calc(items, rate):  # 计算价格
    # 计算小计
    s = sum(item.price for item in items)
    # 计算税
    t = s * rate
    # 返回总计
    return s + t
```

## 测试规范

### 测试命名

```python
# ✓ 正确 - 描述性测试名
def test_user_authentication_fails_with_invalid_credentials():
    pass

def test_rate_limiter_blocks_requests_after_limit_exceeded():
    pass

# ✗ 错误 - 不清晰的测试名
def test_auth():
    pass

def test_1():
    pass
```

### 测试结构

```python
# ✓ 正确 - AAA 模式
def test_http_client_handles_timeout():
    # Arrange
    client = SafeHTTPClient(timeout=1)
    slow_url = "https://httpbin.org/delay/10"
    
    # Act
    with pytest.raises(TimeoutError):
        client.get(slow_url)
    
    # Assert
    # 异常已在 Act 中断言
```

## 代码审查清单

提交代码前检查：

- [ ] 遵循命名规范
- [ ] 添加类型提示
- [ ] 添加文档字符串
- [ ] 删除调试代码
- [ ] 删除未使用的导入
- [ ] 代码格式化（black, isort）
- [ ] 通过 linter 检查
- [ ] 添加测试
- [ ] 更新文档

---

**工具推荐:**

- **Python**: black, isort, flake8, mypy, pylint
- **JavaScript**: ESLint, Prettier
- **通用**: EditorConfig

遵循这些规范将确保代码库的一致性和可维护性。
