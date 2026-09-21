# 修复 13-20: 文件句柄泄漏修复

## 通用修复模式

所有文件都需要将 `open()` 改为使用上下文管理器 `with open()`

### 错误模式和正确模式

#### 模式 1: 单行读取

**❌ 错误**:
```python
content = open(path).read()
data = open(file_path).read()
```

**✅ 正确**:
```python
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()
```

#### 模式 2: JSON 读取

**❌ 错误**:
```python
config = json.load(open(config_path))
```

**✅ 正确**:
```python
with open(config_path, 'r', encoding='utf-8') as f:
    config = json.load(f)
```

#### 模式 3: 二进制读取

**❌ 错误**:
```python
binary_data = open(file_path, 'rb').read()
```

**✅ 正确**:
```python
with open(file_path, 'rb') as f:
    binary_data = f.read()
```

#### 模式 4: 写入

**❌ 错误**:
```python
open(output_path, 'w').write(content)
```

**✅ 正确**:
```python
with open(output_path, 'w', encoding='utf-8') as f:
    f.write(content)
```

---

## 需要修复的文件列表

### 文件 1: `src/agent_manager/core/configuration.py`

**搜索命令**:
```bash
grep -n "open(" src/agent_manager/core/configuration.py | grep -v "with open"
```

**修复所有匹配项**，参考上面的模式。

---

### 文件 2: `src/agent_manager/core/runtime.py`

**搜索命令**:
```bash
grep -n "open(" src/agent_manager/core/runtime.py | grep -v "with open"
```

**修复所有匹配项**。

---

### 文件 3: `src/agent_manager/core/switching.py`

**搜索命令**:
```bash
grep -n "open(" src/agent_manager/core/switching.py | grep -v "with open"
```

**修复所有匹配项**。

---

### 文件 4: `src/agent_manager/accounts/portability.py`

**搜索命令**:
```bash
grep -n "open(" src/agent_manager/accounts/portability.py | grep -v "with open"
```

**修复所有匹配项**。

---

### 文件 5: `src/agent_manager/config/backups.py`

**搜索命令**:
```bash
grep -n "open(" src/agent_manager/config/backups.py | grep -v "with open"
```

**修复所有匹配项**。

---

### 文件 6: `src/agent_manager/sessions/history.py`

**搜索命令**:
```bash
grep -n "open(" src/agent_manager/sessions/history.py | grep -v "with open"
```

**修复所有匹配项**。

---

### 文件 7: `src/agent_manager/integrations/radar.py`

**搜索命令**:
```bash
grep -n "open(" src/agent_manager/integrations/radar.py | grep -v "with open"
```

**修复所有匹配项**。

---

### 文件 8: `src/agent_manager/usage/export.py`

**搜索命令**:
```bash
grep -n "open(" src/agent_manager/usage/export.py | grep -v "with open"
```

**修复所有匹配项**。

---

## 批量验证

修复所有文件后，运行以下命令验证：

```bash
# 检查是否还有泄漏
grep -r "open(" src/agent_manager/ | grep -v "with open" | grep -v ".pyc" | grep -v "__pycache__"

# 运行相关测试
python -m pytest tests/config/ tests/runtime/ tests/sessions/ -v
```

---

## 自动化脚本（可选）

创建一个脚本来辅助查找：

**文件**: `scripts/find_file_leaks.py`

```python
"""查找文件句柄泄漏"""
import re
import sys
from pathlib import Path

def find_leaks(file_path):
    """查找文件中的句柄泄漏"""
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    leaks = []
    for i, line in enumerate(lines, 1):
        # 查找 open( 但不在 with 语句中
        if 'open(' in line and 'with open(' not in line:
            # 排除注释
            if not line.strip().startswith('#'):
                leaks.append((i, line.strip()))
    
    return leaks

if __name__ == '__main__':
    src_dir = Path('src/agent_manager')
    
    for py_file in src_dir.rglob('*.py'):
        leaks = find_leaks(py_file)
        if leaks:
            print(f"\n{py_file}:")
            for line_no, line in leaks:
                print(f"  Line {line_no}: {line[:80]}")
```

**运行**:
```bash
python scripts/find_file_leaks.py
```
