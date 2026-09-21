# 修复 11: 配置文件原子操作

## 文件: `src/agent_manager/core/configuration.py`

**位置**: Lines 64-90，`apply_configuration` 函数

### 步骤 1: 添加导入

在文件顶部添加：

```python
import msvcrt
import os
from contextlib import contextmanager
```

### 步骤 2: 创建文件锁上下文管理器

在文件中任意位置添加：

```python
@contextmanager
def exclusive_file_lock(file_path: str):
    """
    Windows 文件独占锁上下文管理器
    
    使用方法:
        with exclusive_file_lock("config.toml"):
            # 读写配置文件
    """
    lock_file = f"{file_path}.lock"
    
    # 创建锁文件
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    
    try:
        # 获取独占锁（阻塞直到获取）
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        
        yield
        
    finally:
        # 释放锁
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except:
            pass
        
        # 关闭文件描述符
        try:
            os.close(fd)
        except:
            pass
        
        # 清理锁文件
        try:
            if os.path.exists(lock_file):
                os.remove(lock_file)
        except:
            pass
```

### 步骤 3: 修改 apply_configuration 函数

**查找现有的函数**:
```python
def apply_configuration(config_path, updates):
    with CONFIG_FILE_LOCK:
        # 读取
        current = read_toml(config_path)
        # 修改
        current.update(updates)
        # 写入
        write_toml(config_path, current)
```

**替换为**:
```python
def apply_configuration(config_path, updates):
    """原子性地应用配置更新"""
    with CONFIG_FILE_LOCK:
        with exclusive_file_lock(config_path):
            # 读取当前配置
            with open(config_path, 'r', encoding='utf-8') as f:
                content = f.read()
                import toml
                current = toml.loads(content)
            
            # 应用更新
            current.update(updates)
            
            # 原子写入：先写临时文件，再重命名
            temp_path = f"{config_path}.tmp"
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    toml.dump(current, f)
                
                # 原子替换
                os.replace(temp_path, config_path)
            except:
                # 清理临时文件
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise
```

**验证命令**:
```bash
python -m pytest tests/config/test_config_backup_service.py -v
```
