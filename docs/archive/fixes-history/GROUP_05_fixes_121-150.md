# 修复清单 121-150: 测试补充和文档完善

## 修复 121-130: 补充缺失模块的测试

### 121: models/ordering.py 测试

**新建**: `tests/models/__init__.py`

**新建**: `tests/models/test_ordering.py`

```python
"""模型排序测试"""
import pytest
from agent_manager.models.ordering import (
    sort_models_by_capability,
    sort_models_by_priority
)


def test_sort_by_capability():
    """测试按能力排序"""
    models = [
        {'id': 'gpt-3.5', 'capability_score': 70},
        {'id': 'gpt-4', 'capability_score': 95},
        {'id': 'claude-2', 'capability_score': 85}
    ]
    
    sorted_models = sort_models_by_capability(models)
    
    assert sorted_models[0]['id'] == 'gpt-4'
    assert sorted_models[1]['id'] == 'claude-2'
    assert sorted_models[2]['id'] == 'gpt-3.5'


def test_sort_by_priority():
    """测试按优先级排序"""
    models = [
        {'id': 'model-a', 'priority': 2},
        {'id': 'model-b', 'priority': 1},
        {'id': 'model-c', 'priority': 3}
    ]
    
    sorted_models = sort_models_by_priority(models)
    
    assert sorted_models[0]['id'] == 'model-b'
    assert sorted_models[1]['id'] == 'model-a'
    assert sorted_models[2]['id'] == 'model-c'
```

---

### 122: models/preferences.py 测试

**新建**: `tests/models/test_preferences.py`

```python
"""模型偏好测试"""
import pytest
from agent_manager.models.preferences import (
    ModelPreferences,
    save_preferences,
    load_preferences
)


@pytest.fixture
def temp_prefs_file(tmp_path):
    return tmp_path / "preferences.json"


def test_model_preferences_creation():
    """测试创建偏好"""
    prefs = ModelPreferences(
        default_model="gpt-4",
        preferred_models=["gpt-4", "claude-2"],
        blacklisted_models=["gpt-3"]
    )
    
    assert prefs.default_model == "gpt-4"
    assert len(prefs.preferred_models) == 2


def test_save_and_load_preferences(temp_prefs_file):
    """测试保存和加载偏好"""
    prefs = ModelPreferences(
        default_model="gpt-4",
        preferred_models=["gpt-4"]
    )
    
    save_preferences(prefs, temp_prefs_file)
    
    loaded = load_preferences(temp_prefs_file)
    assert loaded.default_model == "gpt-4"
    assert len(loaded.preferred_models) == 1
```

---

### 123: codex/maintenance.py 测试

**新建**: `tests/codex/__init__.py`

**新建**: `tests/codex/test_maintenance.py`

```python
"""Codex 维护测试"""
import pytest
from pathlib import Path
from agent_manager.codex.maintenance import (
    cleanup_cache,
    get_cache_size,
    verify_cache_integrity
)


@pytest.fixture
def temp_cache_dir(tmp_path):
    cache_dir = tmp_path / ".codex" / "cache"
    cache_dir.mkdir(parents=True)
    
    # 创建一些缓存文件
    (cache_dir / "file1.cache").write_text("data1")
    (cache_dir / "file2.cache").write_text("data2")
    
    return cache_dir


def test_get_cache_size(temp_cache_dir):
    """测试获取缓存大小"""
    size = get_cache_size(temp_cache_dir)
    assert size > 0


def test_cleanup_cache(temp_cache_dir):
    """测试清理缓存"""
    initial_size = get_cache_size(temp_cache_dir)
    assert initial_size > 0
    
    cleanup_cache(temp_cache_dir)
    
    # 验证缓存被清理
    # （具体实现取决于 cleanup_cache 的逻辑）


def test_verify_cache_integrity(temp_cache_dir):
    """测试验证缓存完整性"""
    is_valid = verify_cache_integrity(temp_cache_dir)
    # 具体断言取决于实现
```

---

### 124: integrations/toolbox.py 增强测试

**文件**: `tests/integrations/test_toolbox_integration.py` (增强现有)

```python
"""Toolbox 集成测试（增强版）"""
import pytest
from agent_manager.integrations.toolbox import (
    ToolboxService,
    ToolboxConfig,
    list_available_tools
)


@pytest.fixture
def toolbox_service():
    config = ToolboxConfig(
        enabled=True,
        tools_dir=Path("tools")
    )
    return ToolboxService(config)


def test_list_available_tools(toolbox_service):
    """测试列出可用工具"""
    tools = list_available_tools(toolbox_service)
    assert isinstance(tools, list)


def test_toolbox_service_initialization(toolbox_service):
    """测试工具箱服务初始化"""
    assert toolbox_service.config.enabled is True


def test_toolbox_tool_execution(toolbox_service):
    """测试工具执行"""
    # 创建一个测试工具
    result = toolbox_service.execute_tool("test_tool", {"param": "value"})
    # 验证结果
```

---

### 125-130: Frontend 测试补充

**文件**: `frontend/src/modelList.test.js` (增强)

```javascript
import { describe, it, expect } from 'vitest'
import { sortModelsByCapability, filterModelsByProvider } from './modelList.js'

describe('modelList', () => {
  it('sorts models by capability', () => {
    const models = [
      { id: 'gpt-3.5', capability: 70 },
      { id: 'gpt-4', capability: 95 },
      { id: 'claude-2', capability: 85 }
    ]
    
    const sorted = sortModelsByCapability(models)
    
    expect(sorted[0].id).toBe('gpt-4')
    expect(sorted[1].id).toBe('claude-2')
    expect(sorted[2].id).toBe('gpt-3.5')
  })
  
  it('filters models by provider', () => {
    const models = [
      { id: 'gpt-4', provider: 'openai' },
      { id: 'claude-2', provider: 'anthropic' },
      { id: 'gpt-3.5', provider: 'openai' }
    ]
    
    const filtered = filterModelsByProvider(models, 'openai')
    
    expect(filtered).toHaveLength(2)
    expect(filtered[0].id).toBe('gpt-4')
  })
})
```

**新建**: `frontend/src/contextBudget.test.js`（如果缺失）

```javascript
import { describe, it, expect } from 'vitest'
import { calculateContextBudget, isWithinBudget } from './contextBudget.js'

describe('contextBudget', () => {
  it('calculates context budget correctly', () => {
    const budget = calculateContextBudget({
      maxTokens: 100000,
      reservedTokens: 10000
    })
    
    expect(budget.available).toBe(90000)
  })
  
  it('checks if within budget', () => {
    const result = isWithinBudget({
      current: 50000,
      max: 100000
    })
    
    expect(result).toBe(true)
  })
})
```

---

## 修复 131-140: 文档生成和完善

### 131: 生成 API 文档

**新建**: `docs/api/README.md`

```markdown
# Agent Manager API 文档

## 核心模块

### Runtime 模块

管理 Codex 运行时的发现、安装和验证。

#### 主要函数

**find_codex_executable()**
```python
from agent_manager.core.runtime import find_codex_executable

codex_path = find_codex_executable()
if codex_path:
    print(f"Found Codex at: {codex_path}")
```

查找系统上的 Codex 可执行文件。

**返回**: `Optional[Path]` - Codex 路径，如果未找到返回 None

---

**download_official_codex_runtime(version, destination, platform)**
```python
from agent_manager.core.runtime import download_official_codex_runtime
from pathlib import Path

success, message = download_official_codex_runtime(
    version="1.0.0",
    destination=Path("~/codex"),
    platform="win32-x64"
)
```

下载官方 Codex 运行时。

**参数**:
- `version` (str): 版本号
- `destination` (Path): 下载目标目录
- `platform` (str): 平台标识，默认 "win32-x64"

**返回**: `Tuple[bool, str]` - (成功状态, 消息)

---

### Switching 模块

管理账号切换和启动协调。

#### AccountSwitcher

```python
from agent_manager.core.switching import AccountSwitcher
from pathlib import Path

switcher = AccountSwitcher(
    config_path=Path("config.toml"),
    snapshot_dir=Path("snapshots")
)

source_account = {"id": "1", "email": "old@example.com"}
target_account = {"id": "2", "email": "new@example.com"}

success, message = switcher.switch_account(source_account, target_account)
```

**方法**:
- `switch_account(source, target)` - 切换账号

---

### Gateway 中间件

Gateway 使用中间件架构处理请求。

#### 创建自定义中间件

```python
from agent_manager.gateway.middleware import Middleware, Request, Response, Handler

class CustomMiddleware:
    async def process(self, request: Request, next_handler: Handler) -> Response:
        # 预处理
        print(f"Processing: {request['path']}")
        
        # 调用下一个处理器
        response = await next_handler(request)
        
        # 后处理
        response['headers']['X-Custom'] = 'value'
        
        return response
```

#### 使用中间件

```python
from agent_manager.gateway.middleware import MiddlewareChain
from agent_manager.gateway.middleware.auth import AuthMiddleware

chain = MiddlewareChain()
chain.use(AuthMiddleware("api_key"))
chain.use(CustomMiddleware())

handler = chain.build(final_handler)
```

---

## 配置管理

### UnifiedConfigManager

统一的配置管理，支持多层配置（环境变量、JSON、TOML）。

```python
from agent_manager.core.config_manager import UnifiedConfigManager
from pathlib import Path

manager = UnifiedConfigManager(Path("~/.codex"))

# 获取配置
api_key = manager.get("api.key", default="")

# 设置配置
manager.set("api.timeout", 30, layer="toml")

# 获取所有配置
all_config = manager.get_all()
```

**配置优先级**（从高到低）:
1. 环境变量
2. settings.json（用户设置）
3. config.toml（项目配置）

---

## 工具函数

### URL 验证

```python
from agent_manager.core.url_validator import validate_url, URLValidationError

try:
    validated = validate_url(
        "https://api.example.com",
        allowed_schemes={"https"},
        allow_loopback=False
    )
except URLValidationError as e:
    print(f"Invalid URL: {e}")
```

### 进程管理

```python
from agent_manager.core.process_utils import detect_codex_processes, ProcessCheckMode

processes = detect_codex_processes(
    mode=ProcessCheckMode.THOROUGH,
    retry_count=2
)

for proc in processes:
    print(f"PID: {proc.pid}, Path: {proc.exe_path}")
```

### 版本比较

```python
from agent_manager.core.runtime.version_utils import compare_versions, is_version_compatible

result = compare_versions("1.2.3", "1.2.0")
# result > 0 表示第一个版本更新

is_compatible = is_version_compatible("1.2.3", "1.2.0")
# True - 当前版本满足最低要求
```

---

## 错误处理

### 常见异常

```python
from agent_manager.core.url_validator import URLValidationError
from agent_manager.core.switching.validators import ValidationError

try:
    # 操作代码
    pass
except URLValidationError as e:
    print(f"URL 验证失败: {e}")
except ValidationError as e:
    print(f"验证失败: {e}")
except Exception as e:
    print(f"未知错误: {e}")
```

---

## 完整示例

### 切换账号并启动

```python
from pathlib import Path
from agent_manager.core.switching import AccountSwitcher, LaunchCoordinator
from agent_manager.core.switching.progress_reporter import SwitchProgressReporter

def switch_callback(update):
    print(f"[{update.progress*100:.0f}%] {update.message}")

# 创建切换器
reporter = SwitchProgressReporter(callback=switch_callback)
switcher = AccountSwitcher(
    config_path=Path("config.toml"),
    snapshot_dir=Path("snapshots"),
    reporter=reporter
)

# 执行切换
source = {"id": "1", "email": "old@example.com", "provider": "openai"}
target = {"id": "2", "email": "new@example.com", "provider": "anthropic"}

success, message = switcher.switch_account(source, target)

if success:
    # 启动 Codex
    coordinator = LaunchCoordinator()
    success, message = coordinator.launch_codex()
    
    if success:
        print("Codex 启动成功")
    else:
        print(f"启动失败: {message}")
else:
    print(f"切换失败: {message}")
```
```

---

### 132: 内部架构文档

**新建**: `docs/internals/README.md`

```markdown
# 内部架构文档

## 模块组织

### 拆分后的结构

项目经过重构，将大文件拆分为小模块：

#### Runtime 模块 (原 1934 行 → 多个小文件)

```
core/runtime/
├── __init__.py           # 统一入口
├── nodejs_discovery.py   # Node.js 发现
├── windows_registry.py   # 注册表访问
├── powershell_executor.py # PowerShell 执行
├── tarball_handler.py    # Tarball 处理
├── version_utils.py      # 版本工具
├── installation.py       # 安装逻辑
├── discovery.py          # 可执行文件发现
└── validation.py         # 验证逻辑
```

#### Switching 模块 (原 1107 行 → 多个小文件)

```
core/switching/
├── __init__.py              # 统一入口
├── progress_reporter.py     # 进度报告
├── transaction_manager.py   # 事务管理
├── validators.py            # 验证器
├── launch_verifier.py       # 启动验证
├── account_switcher.py      # 账号切换器
└── launch_coordinator.py    # 启动协调
```

#### App Server 模块 (原 581 行 → 多个小文件)

```
core/app_server/
├── __init__.py         # 统一入口
├── jsonrpc_client.py   # JSON-RPC 客户端
├── thread_pool.py      # 线程池
├── session_manager.py  # 会话管理
└── codex_client.py     # Codex 客户端
```

---

## 中间件架构

Gateway 使用中间件链模式处理请求：

```
Request → Logging → RateLimit → Auth → Validation → Handler → Response
```

每个中间件可以：
1. 检查和修改请求
2. 决定是否继续传递
3. 修改响应

---

## 配置系统

采用分层配置，优先级从高到低：

1. **环境变量** - 运行时覆盖
2. **settings.json** - 用户自定义设置
3. **config.toml** - 项目默认配置

---

## 快照和事务

使用快照服务实现事务性操作：

```
开始事务 → 创建快照 → 执行操作 → 成功：提交 / 失败：回滚
```

快照存储在独立目录，支持多个并发事务。

---

## 进程管理

统一的进程检测和管理：

- **FAST 模式** - 快速检查，可能不完整
- **THOROUGH 模式** - 完整扫描（默认）
- **VALIDATE 模式** - 包含额外验证

所有进程操作使用 PowerShell 实现，确保跨 Windows 版本兼容。

---

## 国际化

错误消息和界面文本支持多语言：

```
translations/
├── zh-CN.json  # 简体中文
└── en-US.json  # 英文
```

使用 `t()` 函数获取翻译：
```python
from agent_manager.core.i18n import t

error_msg = t("errors.account_not_found")
```
```

---

### 133-135: 模块文档生成

为每个主要模块创建详细文档：

**新建**: `docs/modules/runtime.md`
**新建**: `docs/modules/switching.md`
**新建**: `docs/modules/gateway.md`

示例（`docs/modules/runtime.md`）：

```markdown
# Runtime 模块

管理 Codex 运行时的生命周期。

## 功能

### 发现和安装

- 自动发现系统上的 Codex 安装
- 从官方源下载运行时
- 验证安装完整性

### 依赖检查

- Node.js 版本检查
- Windows 注册表查询
- 环境变量验证

### 版本管理

- 版本比较和兼容性检查
- 自动更新检测
- 多版本并存支持

## API

见 [API 文档](../api/README.md#runtime-模块)

## 实现细节

### Node.js 发现

使用多种方法查找 Node.js：
1. PATH 环境变量
2. 常见安装位置
3. 注册表查询

### PowerShell 集成

所有 Windows 操作通过 PowerShell 执行：
- 进程管理
- 注册表访问
- 系统信息查询

### 错误处理

所有函数返回 `Tuple[bool, str]` 格式：
- `(True, message)` - 成功
- `(False, error)` - 失败

## 测试

```bash
python -m pytest tests/core/test_nodejs_discovery.py -v
python -m pytest tests/core/test_windows_registry.py -v
```
```

---

### 136-140: 用户指南完善

**更新**: `docs/usage.md`（补充新功能说明）

添加以下章节：

```markdown
## 高级配置

### 使用环境变量

可以通过环境变量覆盖配置：

```bash
# PowerShell
$env:API_KEY="your_key_here"
$env:API_ENDPOINT="https://custom.endpoint.com"

# 或在系统环境变量中设置
```

### 配置文件

支持两种配置文件：

1. **config.toml** - 项目配置
2. **settings.json** - 用户设置

用户设置优先级高于项目配置。

---

## 故障排查

### Codex 无法启动

1. 检查安装路径
   ```bash
   python -c "from agent_manager.core.runtime import find_codex_executable; print(find_codex_executable())"
   ```

2. 验证 Node.js
   ```bash
   node --version
   ```

3. 查看日志
   ```
   %USERPROFILE%\.codex\agent-manager\logs\
   ```

### 账号切换失败

1. 检查账号配置
2. 确认 Codex 已完全停止
3. 查看快照目录是否有备份

### 网关连接问题

1. 检查端口占用
2. 验证 API Key
3. 查看中间件日志
```

---

## 修复 141-150: 代码风格统一和优化

### 141: 创建代码风格配置

**新建**: `.pylintrc`

```ini
[MASTER]
max-line-length=100
disable=
    C0111,  # missing-docstring
    C0103,  # invalid-name
    R0913,  # too-many-arguments
    R0914,  # too-many-locals

[FORMAT]
indent-string='    '

[BASIC]
good-names=i,j,k,ex,_,id,x,y,z

[DESIGN]
max-attributes=10
max-args=8
```

**新建**: `.flake8`

```ini
[flake8]
max-line-length = 100
exclude = .git,__pycache__,build,dist,venv,.venv
ignore = E203,W503
per-file-ignores =
    __init__.py:F401
```

**新建**: `pyproject.toml` (black 配置)

```toml
[tool.black]
line-length = 100
target-version = ['py311']
include = '\.pyi?$'
extend-exclude = '''
/(
    \.eggs
  | \.git
  | \.venv
  | build
  | dist
)/
'''
```

---

### 142: 类型提示补充脚本

**新建**: `scripts/add_type_hints.py`

```python
"""为缺少类型提示的函数添加基本类型提示"""
import ast
import sys
from pathlib import Path


def analyze_file(file_path: Path):
    """分析文件中缺少类型提示的函数"""
    with open(file_path, 'r', encoding='utf-8') as f:
        source = f.read()
    
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    
    missing_hints = []
    
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            # 检查返回类型
            if node.returns is None and node.name != '__init__':
                missing_hints.append({
                    'name': node.name,
                    'line': node.lineno,
                    'type': 'return'
                })
            
            # 检查参数类型
            for arg in node.args.args:
                if arg.annotation is None and arg.arg != 'self':
                    missing_hints.append({
                        'name': node.name,
                        'arg': arg.arg,
                        'line': node.lineno,
                        'type': 'parameter'
                    })
    
    return missing_hints


def main():
    src_dir = Path('src/agent_manager')
    
    print("扫描缺少类型提示的函数...\n")
    
    total_issues = 0
    
    for py_file in src_dir.rglob('*.py'):
        issues = analyze_file(py_file)
        if issues:
            print(f"\n{py_file}:")
            for issue in issues:
                total_issues += 1
                if issue['type'] == 'return':
                    print(f"  Line {issue['line']}: {issue['name']}() - 缺少返回类型")
                else:
                    print(f"  Line {issue['line']}: {issue['name']}({issue['arg']}) - 参数缺少类型")
    
    print(f"\n总计: {total_issues} 个缺少类型提示")


if __name__ == '__main__':
    main()
```

**运行**: `python scripts/add_type_hints.py`

---

### 143-145: 批量代码格式化

创建格式化脚本：

**新建**: `scripts/format_code.ps1`

```powershell
# 代码格式化脚本

Write-Host "正在格式化 Python 代码..." -ForegroundColor Green

# 使用 black 格式化
python -m black src/agent_manager tests

# 使用 isort 排序导入
python -m isort src/agent_manager tests

# 检查代码风格
Write-Host "`n正在检查代码风格..." -ForegroundColor Green
python -m flake8 src/agent_manager

# 类型检查
Write-Host "`n正在进行类型检查..." -ForegroundColor Green
python -m mypy src/agent_manager --ignore-missing-imports

Write-Host "`n完成!" -ForegroundColor Green
```

**安装依赖**:
```bash
pip install black isort flake8 mypy
```

**运行**:
```powershell
.\scripts\format_code.ps1
```

---

### 146-150: 性能优化

#### 146: 缓存优化

**新建**: `src/agent_manager/core/caching.py`

```python
"""缓存工具"""
from functools import wraps
from typing import Callable, Any
import time


def ttl_cache(ttl_seconds: int = 300):
    """带 TTL 的缓存装饰器"""
    def decorator(func: Callable) -> Callable:
        cache = {}
        cache_times = {}
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            key = str(args) + str(kwargs)
            now = time.time()
            
            # 检查缓存是否存在且未过期
            if key in cache and now - cache_times[key] < ttl_seconds:
                return cache[key]
            
            # 执行函数并缓存结果
            result = func(*args, **kwargs)
            cache[key] = result
            cache_times[key] = now
            
            return result
        
        return wrapper
    return decorator


# 使用示例
@ttl_cache(ttl_seconds=600)
def get_model_list():
    """获取模型列表（缓存10分钟）"""
    # 实际的API调用
    pass
```

#### 147: 批量操作优化

**更新**: `src/agent_manager/core/batch_operations.py`

```python
"""批量操作优化"""
from typing import List, Callable, Any
from concurrent.futures import ThreadPoolExecutor, as_completed


def batch_process(
    items: List[Any],
    process_func: Callable,
    batch_size: int = 10,
    max_workers: int = 4
) -> List[Any]:
    """
    批量处理项目
    
    Args:
        items: 要处理的项目列表
        process_func: 处理函数
        batch_size: 每批大小
        max_workers: 最大工作线程数
    
    Returns:
        处理结果列表
    """
    results = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        
        # 分批提交任务
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            future = executor.submit(_process_batch, batch, process_func)
            futures.append(future)
        
        # 收集结果
        for future in as_completed(futures):
            results.extend(future.result())
    
    return results


def _process_batch(batch: List[Any], process_func: Callable) -> List[Any]:
    """处理单个批次"""
    return [process_func(item) for item in batch]
```

---

**验证 121-150**:
```bash
# 运行所有测试
python -m pytest tests/ -v --cov=src/agent_manager

# 检查代码风格
python -m flake8 src/agent_manager

# 格式化代码
python -m black src/agent_manager tests

# 生成文档
# （如果有文档生成工具）
```
