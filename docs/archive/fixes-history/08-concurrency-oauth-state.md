# 修复 12: OAuth 状态管理线程安全

## 文件: `src/agent_manager/application/oauth.py`

**位置**: Lines 486-500，超时检查逻辑

### 步骤 1: 确保所有状态访问都在锁保护下

**查找类似代码**:
```python
# 检查状态
if self._current_state:
    # 超时检查
    if time.time() - self._current_state['timestamp'] > TIMEOUT:
        # 清理状态
        self._current_state = None
```

**替换为**:
```python
# 所有状态访问必须在锁保护下
with self._state_lock:
    if self._current_state:
        # 超时检查
        if time.time() - self._current_state['timestamp'] > TIMEOUT:
            # 清理状态
            self._current_state = None
```

### 步骤 2: 创建状态访问辅助方法

在 OAuth 类中添加这些方法（查找类定义）:

```python
def _get_state_safe(self, state_id: str) -> dict:
    """线程安全地获取状态"""
    with self._state_lock:
        if self._current_state and self._current_state.get('id') == state_id:
            return self._current_state.copy()
        return None


def _set_state_safe(self, state_data: dict):
    """线程安全地设置状态"""
    with self._state_lock:
        self._current_state = state_data.copy()


def _clear_state_safe(self, state_id: str = None):
    """线程安全地清除状态"""
    with self._state_lock:
        if state_id is None or (
            self._current_state and 
            self._current_state.get('id') == state_id
        ):
            self._current_state = None
```

### 步骤 3: 替换所有直接状态访问

**搜索文件中所有 `self._current_state` 的使用**:

```bash
# 在文件中搜索
grep -n "self._current_state" src/agent_manager/application/oauth.py
```

**替换规则**:
- 读取状态: `self._current_state` → `self._get_state_safe(state_id)`
- 设置状态: `self._current_state = new_state` → `self._set_state_safe(new_state)`
- 清除状态: `self._current_state = None` → `self._clear_state_safe(state_id)`

**示例**:

**修改前**:
```python
def check_callback(self, state_id):
    if self._current_state and self._current_state['id'] == state_id:
        return self._current_state['data']
```

**修改后**:
```python
def check_callback(self, state_id):
    state = self._get_state_safe(state_id)
    if state:
        return state['data']
```

**验证命令**:
```bash
python -m pytest tests/accounts/test_oauth_lifecycle.py -v
```
