# 修复清单 031-060: 重构使用统一工具

## 修复 031: 重构 switching.py 使用统一 HTTP 客户端

**文件**: `src/agent_manager/core/switching.py`

### 31.1 替换导入
查找并删除旧的 urllib 导入，添加新导入：
```python
from agent_manager.core.http_client import create_same_origin_client, SafeHTTPClient
```

### 31.2 查找并替换 _SameOriginRedirectHandler 的使用（约1013-1040行）
```python
# 删除自定义类定义
class _SameOriginRedirectHandler(HTTPRedirectHandler):
    # ... 删除整个类

# 替换使用处
# 修改前:
handler = _SameOriginRedirectHandler(origin_url)
opener = build_opener(handler)
response = opener.open(request)

# 修改后:
client = create_same_origin_client(origin_url)
response_data = client.get(url)
```

### 31.3 替换所有 _open_same_origin_request 调用
```bash
# 搜索: grep -n "_open_same_origin_request" src/agent_manager/core/switching.py
# 每个调用都替换为 HTTP 客户端方法
```

---

## 修复 032: 重构 runtime.py 使用统一 HTTP 客户端

**文件**: `src/agent_manager/core/runtime.py`

### 32.1 替换 _registry_json 函数（约243-266行）
```python
from agent_manager.core.http_client import create_registry_client

def _registry_json(url: str, proxy: Optional[str] = None) -> dict:
    """从 npm registry 获取 JSON 数据"""
    client = create_registry_client(proxy=proxy)
    return client.get_json(url)
```

### 32.2 查找所有 urlopen 调用并替换
```bash
grep -n "urlopen" src/agent_manager/core/runtime.py
# 替换为统一客户端
```

---

## 修复 033: 重构 integrations/radar.py 使用统一 HTTP 客户端

**文件**: `src/agent_manager/integrations/radar.py`

### 33.1 添加导入
```python
from agent_manager.core.http_client import create_api_client
```

### 33.2 替换所有 HTTP 调用
```python
# 修改前:
response = urllib.request.urlopen(url, timeout=30)
data = json.loads(response.read())

# 修改后:
client = create_api_client()
data = client.get_json(url)
```

---

## 修复 034: 重构 accounts/relay.py 使用统一 HTTP 客户端

**文件**: `src/agent_manager/accounts/relay.py`

### 34.1 替换所有 HTTP 请求
```python
from agent_manager.core.http_client import SafeHTTPClient

# 创建客户端实例
client = SafeHTTPClient(timeout=30)

# 替换所有 urlopen 为 client.get() 或 client.post()
```

---

## 修复 035: 重构 switching.py 使用统一进程工具

**文件**: `src/agent_manager/core/switching.py`

### 35.1 添加导入
```python
from agent_manager.core.process_utils import (
    detect_codex_processes,
    ProcessCheckMode,
    kill_process_tree,
    wait_for_process_exit
)
```

### 35.2 替换进程检测调用
```bash
# 搜索: grep -n "_running_windows_codex_candidates\|running_codex_processes" src/agent_manager/core/switching.py

# 修改前:
processes = _running_windows_codex_candidates()

# 修改后:
processes = detect_codex_processes(mode=ProcessCheckMode.THOROUGH)
```

### 35.3 替换进程终止调用
```python
# 修改前:
# 自定义的进程终止逻辑

# 修改后:
success = kill_process_tree(pid, timeout=5)
if success:
    wait_for_process_exit(pid, timeout=30)
```

---

## 修复 036: 重构 processes.py 使用统一进程工具

**文件**: `src/agent_manager/core/processes.py`

### 36.1 删除重复的进程检测函数
```python
# 删除以下函数（如果存在）:
# - _running_windows_codex_candidates()
# - _require_known_codex_processes()
# - 其他重复的检测逻辑
```

### 36.2 使用统一工具
```python
from agent_manager.core.process_utils import detect_codex_processes, ProcessCheckMode

def get_codex_processes():
    return detect_codex_processes(
        mode=ProcessCheckMode.THOROUGH,
        retry_count=2,
        retry_delay=1.0
    )
```

---

## 修复 037: 重构 application/processes.py 使用统一进程工具

**文件**: `src/agent_manager/application/processes.py`

### 37.1 替换 _codex_launch_process_observation
```python
from agent_manager.core.process_utils import detect_codex_processes, ProcessCheckMode

def _codex_launch_process_observation():
    """观察启动的进程"""
    return detect_codex_processes(
        mode=ProcessCheckMode.VALIDATE,
        retry_count=3,
        retry_delay=0.5
    )
```

---

## 修复 038: 重构 switching.py 使用统一 URL 验证

**文件**: `src/agent_manager/core/switching.py`

### 38.1 添加导入
```python
from agent_manager.core.url_validator import (
    validate_provider_url,
    validate_provider_portal_url,
    URLValidationError
)
```

### 38.2 替换 _validated_provider_url（约915-959行）
```python
# 删除旧函数，使用新的验证器
def _validated_provider_url(url: str) -> str:
    try:
        return validate_provider_url(url)
    except URLValidationError as e:
        raise ValueError(str(e))
```

### 38.3 替换 _validated_provider_portal_url（约963-981行）
```python
def _validated_provider_portal_url(url: str) -> str:
    try:
        return validate_provider_portal_url(url)
    except URLValidationError as e:
        raise ValueError(str(e))
```

### 38.4 删除 _validated_provider_related_url（约1087-1106行）
```python
# 如果这个函数与上面两个类似，直接使用 validate_provider_url
```

---

## 修复 039: 重构 accounts/relay.py 使用统一 URL 验证

**文件**: `src/agent_manager/accounts/relay.py`

### 39.1 添加导入并替换验证
```python
from agent_manager.core.url_validator import validate_url, URLValidationError

# 在所有需要验证 URL 的地方使用
try:
    validated_url = validate_url(user_provided_url)
except URLValidationError as e:
    # 处理错误
```

---

## 修复 040: 重构 gateway/service.py 使用统一 URL 验证

**文件**: `src/agent_manager/gateway/service.py`

### 40.1 验证上游端点
```python
from agent_manager.core.url_validator import validate_url

def configure_upstream(self, endpoint_url: str):
    """配置上游端点"""
    validated = validate_url(
        endpoint_url,
        allowed_schemes={'http', 'https'},
        purpose="upstream endpoint"
    )
    self.upstream_url = validated
```

---

## 修复 041: 创建快照服务

**新建**: `src/agent_manager/storage/snapshot_service.py`

```python
"""统一的快照/备份服务"""
import hashlib
import json
import time
from dataclasses import dataclass, asdict
from typing import Protocol, Any, Optional
from pathlib import Path


@dataclass
class Snapshot:
    content_hash: str
    timestamp: float
    data: Any
    metadata: dict
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict):
        return cls(**data)


class SnapshotProvider(Protocol):
    def capture(self) -> dict: ...
    def restore(self, data: dict) -> None: ...


class SnapshotService:
    def __init__(self, storage_dir: Path):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
    
    def create_snapshot(self, provider: SnapshotProvider, metadata: Optional[dict] = None) -> Snapshot:
        data = provider.capture()
        content = json.dumps(data, sort_keys=True, ensure_ascii=False)
        content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        
        return Snapshot(
            content_hash=content_hash,
            timestamp=time.time(),
            data=data,
            metadata=metadata or {}
        )
    
    def save_snapshot(self, snapshot: Snapshot, name: str):
        snapshot_file = self.storage_dir / f"{name}.json"
        with open(snapshot_file, 'w', encoding='utf-8') as f:
            json.dump(snapshot.to_dict(), f, indent=2, ensure_ascii=False)
    
    def load_snapshot(self, name: str) -> Optional[Snapshot]:
        snapshot_file = self.storage_dir / f"{name}.json"
        if not snapshot_file.exists():
            return None
        with open(snapshot_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return Snapshot.from_dict(data)
    
    def restore_snapshot(self, snapshot: Snapshot, provider: SnapshotProvider):
        provider.restore(snapshot.data)
    
    def list_snapshots(self) -> list:
        return [f.stem for f in self.storage_dir.glob("*.json")]
    
    def delete_snapshot(self, name: str) -> bool:
        snapshot_file = self.storage_dir / f"{name}.json"
        if snapshot_file.exists():
            snapshot_file.unlink()
            return True
        return False
    
    def cleanup_old_snapshots(self, keep_count: int = 3):
        snapshots = []
        for snapshot_file in self.storage_dir.glob("*.json"):
            with open(snapshot_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                snapshots.append((snapshot_file.name, data.get('timestamp', 0)))
        
        snapshots.sort(key=lambda x: x[1], reverse=True)
        
        for name, _ in snapshots[keep_count:]:
            snapshot_file = self.storage_dir / name
            snapshot_file.unlink()
```

---

## 修复 042: 快照服务配置提供者示例

**继续在**: `src/agent_manager/storage/snapshot_service.py`

```python
class ConfigSnapshotProvider:
    """配置文件快照提供者"""
    
    def __init__(self, config_path: Path):
        self.config_path = config_path
    
    def capture(self) -> dict:
        with open(self.config_path, 'r', encoding='utf-8') as f:
            import toml
            return toml.load(f)
    
    def restore(self, data: dict):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            import toml
            toml.dump(data, f)


class AccountSnapshotProvider:
    """账号快照提供者"""
    
    def __init__(self, accounts_data: dict):
        self.accounts_data = accounts_data
    
    def capture(self) -> dict:
        return self.accounts_data.copy()
    
    def restore(self, data: dict):
        self.accounts_data.clear()
        self.accounts_data.update(data)
```

---

## 修复 043: 快照服务测试

**新建**: `tests/storage/__init__.py`

**新建**: `tests/storage/test_snapshot_service.py`

```python
import pytest
import json
from pathlib import Path
from agent_manager.storage.snapshot_service import (
    SnapshotService,
    Snapshot,
    ConfigSnapshotProvider
)


@pytest.fixture
def temp_storage(tmp_path):
    return tmp_path / "snapshots"


@pytest.fixture
def snapshot_service(temp_storage):
    return SnapshotService(temp_storage)


def test_create_snapshot(snapshot_service, tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[section]\nkey = 'value'")
    
    provider = ConfigSnapshotProvider(config_file)
    snapshot = snapshot_service.create_snapshot(provider)
    
    assert snapshot.content_hash
    assert snapshot.timestamp > 0
    assert 'section' in snapshot.data


def test_save_load_snapshot(snapshot_service, tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[section]\nkey = 'value'")
    
    provider = ConfigSnapshotProvider(config_file)
    snapshot = snapshot_service.create_snapshot(provider)
    
    snapshot_service.save_snapshot(snapshot, "test")
    
    loaded = snapshot_service.load_snapshot("test")
    assert loaded.content_hash == snapshot.content_hash
    assert loaded.data == snapshot.data


def test_list_snapshots(snapshot_service):
    snapshot1 = Snapshot("hash1", 1.0, {"a": 1}, {})
    snapshot2 = Snapshot("hash2", 2.0, {"b": 2}, {})
    
    snapshot_service.save_snapshot(snapshot1, "snap1")
    snapshot_service.save_snapshot(snapshot2, "snap2")
    
    snapshots = snapshot_service.list_snapshots()
    assert "snap1" in snapshots
    assert "snap2" in snapshots


def test_cleanup_old_snapshots(snapshot_service):
    for i in range(5):
        snapshot = Snapshot(f"hash{i}", float(i), {"data": i}, {})
        snapshot_service.save_snapshot(snapshot, f"snap{i}")
    
    snapshot_service.cleanup_old_snapshots(keep_count=3)
    
    remaining = snapshot_service.list_snapshots()
    assert len(remaining) == 3
```

---

## 修复 044: 重构 identity.py 使用快照服务

**文件**: `src/agent_manager/core/identity.py`

### 44.1 替换 _capture_switch_transaction_snapshot
```python
from agent_manager.storage.snapshot_service import SnapshotService, AccountSnapshotProvider
from agent_manager.paths import get_data_dir

def _capture_switch_transaction_snapshot(accounts: dict) -> dict:
    """捕获账号切换快照"""
    snapshot_dir = get_data_dir() / "snapshots" / "transactions"
    service = SnapshotService(snapshot_dir)
    
    provider = AccountSnapshotProvider(accounts)
    snapshot = service.create_snapshot(provider, metadata={
        "type": "switch_transaction",
        "timestamp": time.time()
    })
    
    snapshot_name = f"switch_{int(time.time())}"
    service.save_snapshot(snapshot, snapshot_name)
    
    return snapshot.to_dict()
```

---

## 修复 045: 重构 config/backups.py 使用快照服务

**文件**: `src/agent_manager/config/backups.py`

### 45.1 集成快照服务
```python
from agent_manager.storage.snapshot_service import SnapshotService, ConfigSnapshotProvider

class ConfigBackupManager:
    def __init__(self, config_path: Path, backup_dir: Path):
        self.config_path = config_path
        self.snapshot_service = SnapshotService(backup_dir)
        self.provider = ConfigSnapshotProvider(config_path)
    
    def create_backup(self, backup_name: str):
        """创建配置备份"""
        snapshot = self.snapshot_service.create_snapshot(
            self.provider,
            metadata={"manual": True}
        )
        self.snapshot_service.save_snapshot(snapshot, backup_name)
        return snapshot
    
    def restore_backup(self, backup_name: str):
        """恢复配置备份"""
        snapshot = self.snapshot_service.load_snapshot(backup_name)
        if snapshot:
            self.snapshot_service.restore_snapshot(snapshot, self.provider)
            return True
        return False
```

---

## 修复 046: 重构 storage/coordinator.py 使用快照服务

**文件**: `src/agent_manager/storage/coordinator.py`

### 46.1 使用统一快照进行事务
```python
from agent_manager.storage.snapshot_service import SnapshotService

class StorageCoordinator:
    def __init__(self):
        self.snapshot_service = SnapshotService(Path("data/coordinator_snapshots"))
    
    def begin_transaction(self, provider):
        """开始事务，创建快照"""
        snapshot = self.snapshot_service.create_snapshot(provider)
        self.snapshot_service.save_snapshot(snapshot, "transaction_backup")
        return snapshot
    
    def commit_transaction(self):
        """提交事务，删除快照"""
        self.snapshot_service.delete_snapshot("transaction_backup")
    
    def rollback_transaction(self, provider):
        """回滚事务，恢复快照"""
        snapshot = self.snapshot_service.load_snapshot("transaction_backup")
        if snapshot:
            self.snapshot_service.restore_snapshot(snapshot, provider)
```

---

## 修复 047-050: 创建国际化系统

**新建**: `src/agent_manager/core/i18n.py`

```python
"""国际化支持"""
from typing import Dict
import json
from pathlib import Path


class I18n:
    def __init__(self, locale: str = "zh-CN"):
        self.locale = locale
        self.translations: Dict[str, Dict[str, str]] = {}
        self.load_translations()
    
    def load_translations(self):
        trans_file = Path(__file__).parent / "translations" / f"{self.locale}.json"
        if trans_file.exists():
            with open(trans_file, 'r', encoding='utf-8') as f:
                self.translations = json.load(f)
    
    def t(self, key: str, **kwargs) -> str:
        """翻译键"""
        keys = key.split('.')
        current = self.translations
        
        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return key
        
        if isinstance(current, str):
            return current.format(**kwargs)
        return key
    
    def set_locale(self, locale: str):
        """切换语言"""
        self.locale = locale
        self.load_translations()


# 全局实例
_i18n = I18n()

def t(key: str, **kwargs) -> str:
    """全局翻译函数"""
    return _i18n.t(key, **kwargs)

def set_locale(locale: str):
    """设置语言"""
    _i18n.set_locale(locale)
```

**新建**: `src/agent_manager/core/translations/__init__.py`

**新建**: `src/agent_manager/core/translations/zh-CN.json`

```json
{
  "errors": {
    "account_not_found": "账号不存在",
    "invalid_runtime_url": "官方 Codex 运行时元数据地址无效",
    "batch_request_range": "Codex App Server 批量请求数量必须在 {min} 到 {max} 之间",
    "config_read_failed": "配置读取失败: {error}",
    "config_write_failed": "配置写入失败: {error}",
    "network_timeout": "网络请求超时",
    "auth_failed": "认证失败",
    "invalid_credentials": "无效的凭据"
  },
  "messages": {
    "starting_server": "正在启动服务器...",
    "server_started": "服务器已启动",
    "stopping_server": "正在停止服务器...",
    "server_stopped": "服务器已停止",
    "switching_account": "正在切换账号...",
    "account_switched": "账号切换成功"
  },
  "validation": {
    "required_field": "必填字段",
    "invalid_format": "格式无效",
    "min_length": "最小长度为 {min}",
    "max_length": "最大长度为 {max}"
  }
}
```

**新建**: `src/agent_manager/core/translations/en-US.json`

```json
{
  "errors": {
    "account_not_found": "Account not found",
    "invalid_runtime_url": "Invalid Codex runtime metadata URL",
    "batch_request_range": "Codex App Server batch request count must be between {min} and {max}",
    "config_read_failed": "Configuration read failed: {error}",
    "config_write_failed": "Configuration write failed: {error}",
    "network_timeout": "Network request timeout",
    "auth_failed": "Authentication failed",
    "invalid_credentials": "Invalid credentials"
  },
  "messages": {
    "starting_server": "Starting server...",
    "server_started": "Server started",
    "stopping_server": "Stopping server...",
    "server_stopped": "Server stopped",
    "switching_account": "Switching account...",
    "account_switched": "Account switched successfully"
  },
  "validation": {
    "required_field": "Required field",
    "invalid_format": "Invalid format",
    "min_length": "Minimum length is {min}",
    "max_length": "Maximum length is {max}"
  }
}
```

---

## 修复 051: 替换 app_server.py 中的中文错误消息

**文件**: `src/agent_manager/core/app_server.py`

### 51.1 添加导入
```python
from agent_manager.core.i18n import t
```

### 51.2 替换所有中文错误消息
```bash
# 搜索: grep -n "Codex App Server 批量请求" src/agent_manager/core/app_server.py

# 修改前:
raise ValueError("Codex App Server 批量请求数量必须在 1 到 200 之间")

# 修改后:
raise ValueError(t("errors.batch_request_range", min=1, max=200))
```

---

## 修复 052: 替换 runtime.py 中的中文错误消息

**文件**: `src/agent_manager/core/runtime.py`

```python
from agent_manager.core.i18n import t

# 修改前:
raise ValueError("官方 Codex 运行时元数据地址无效")

# 修改后:
raise ValueError(t("errors.invalid_runtime_url"))
```

---

## 修复 053: 替换 switching.py 中的中文错误消息

**文件**: `src/agent_manager/core/switching.py`

```python
from agent_manager.core.i18n import t

# 修改前:
raise ValueError("账号不存在")

# 修改后:
raise ValueError(t("errors.account_not_found"))
```

---

## 修复 054: I18n 测试

**新建**: `tests/core/test_i18n.py`

```python
import pytest
from agent_manager.core.i18n import I18n, t, set_locale


def test_translation_basic():
    i18n = I18n(locale="zh-CN")
    result = i18n.t("errors.account_not_found")
    assert result == "账号不存在"


def test_translation_with_params():
    i18n = I18n(locale="zh-CN")
    result = i18n.t("errors.batch_request_range", min=1, max=200)
    assert "1" in result and "200" in result


def test_missing_key():
    i18n = I18n(locale="zh-CN")
    result = i18n.t("nonexistent.key")
    assert result == "nonexistent.key"


def test_switch_locale():
    i18n = I18n(locale="zh-CN")
    assert "账号" in i18n.t("errors.account_not_found")
    
    i18n.set_locale("en-US")
    assert "Account" in i18n.t("errors.account_not_found")


def test_global_translation_function():
    set_locale("zh-CN")
    assert "账号" in t("errors.account_not_found")
```

---

## 修复 055-060: Windows Store 路径统一检查

**新建**: `src/agent_manager/platform/paths.py`（如果不存在）

```python
"""平台路径工具"""
from pathlib import Path
import re

_WINDOWS_STORE_PATTERN = re.compile(
    r'\\WindowsApps\\|\\Microsoft\.WindowsStore_'
)


def is_windows_store_path(path: str | Path) -> bool:
    """检查路径是否为 Windows Store 应用路径"""
    path_str = str(path).lower()
    return bool(_WINDOWS_STORE_PATTERN.search(path_str))


def normalize_path(path: str | Path) -> Path:
    """规范化路径"""
    return Path(path).resolve()


def ensure_writable_directory(path: Path) -> bool:
    """确保目录可写"""
    try:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        
        test_file = path / ".write_test"
        test_file.touch()
        test_file.unlink()
        return True
    except:
        return False
```

### 055: 重构 runtime.py 使用统一路径工具（约1463-1467行）
```python
from agent_manager.platform.paths import is_windows_store_path

# 删除本地实现，使用统一函数
if is_windows_store_path(exe_path):
    # 处理逻辑
```

### 056: 重构 switching.py 使用统一路径工具
```python
from agent_manager.platform.paths import is_windows_store_path

# 替换所有本地的 Windows Store 检查
```

### 057: 重构 processes.py 使用统一路径工具
```python
from agent_manager.platform.paths import is_windows_store_path

# 替换进程路径检查
```

### 058: 平台路径工具测试

**新建**: `tests/platform/__init__.py`

**新建**: `tests/platform/test_paths.py`

```python
import pytest
from pathlib import Path
from agent_manager.platform.paths import (
    is_windows_store_path,
    normalize_path,
    ensure_writable_directory
)


def test_windows_store_path_detection():
    assert is_windows_store_path("C:\\Program Files\\WindowsApps\\App") is True
    assert is_windows_store_path("C:\\Users\\Test\\Microsoft.WindowsStore_123") is True
    assert is_windows_store_path("C:\\Program Files\\Normal\\App") is False


def test_normalize_path():
    path = normalize_path("./relative/path")
    assert path.is_absolute()


def test_ensure_writable_directory(tmp_path):
    test_dir = tmp_path / "test_writable"
    assert ensure_writable_directory(test_dir) is True
    assert test_dir.exists()
```

### 059-060: 清理冗余的路径检查函数

**批量搜索并删除**:
```bash
# 搜索所有 _is_windows_store_path 定义
grep -rn "_is_windows_store_path\|isWindowsStorePath" src/agent_manager/

# 逐个删除重复定义，替换为统一导入
```

---

**验证 031-060**:
```bash
python -m pytest tests/core/ tests/storage/ tests/platform/ -v
python -m agent_manager  # 确保程序能启动
```
