# 贡献指南

感谢您对 Agent Manager 项目的关注！我们欢迎各种形式的贡献。

## 📋 目录

- [行为准则](#行为准则)
- [如何贡献](#如何贡献)
- [开发设置](#开发设置)
- [提交指南](#提交指南)
- [代码规范](#代码规范)
- [测试要求](#测试要求)
- [文档](#文档)

## 行为准则

本项目采用贡献者公约。参与本项目即表示您同意遵守其条款。

### 我们的承诺

为了营造开放和友好的环境，我们承诺：
- 使用友好和包容的语言
- 尊重不同的观点和经验
- 优雅地接受建设性批评
- 关注对社区最有利的事情
- 对其他社区成员表示同理心

## 如何贡献

### 报告 Bug

在提交 Bug 报告之前：
1. 检查 [现有 Issues](https://github.com/Foxgh127/Agent-Manager/issues)
2. 确保使用最新版本
3. 收集相关信息

**Bug 报告应包含：**
- 清晰的标题
- 详细的描述
- 重现步骤
- 预期行为
- 实际行为
- 环境信息 (操作系统、Python 版本等)
- 日志或截图

### 建议新功能

在提交功能建议之前：
1. 检查是否已有类似建议
2. 确保功能符合项目目标

**功能建议应包含：**
- 清晰的用例
- 建议的实现方案
- 可能的替代方案
- 对现有功能的影响

### 提交 Pull Request

1. **Fork 仓库**
   ```bash
   git clone https://github.com/YOUR_USERNAME/Agent-Manager.git
   cd Agent-Manager
   git remote add upstream https://github.com/Foxgh127/Agent-Manager.git
   ```

2. **创建分支**
   ```bash
   git checkout -b feature/your-feature-name
   # 或
   git checkout -b fix/your-bug-fix
   ```

3. **进行更改**
   - 遵循代码规范
   - 添加测试
   - 更新文档

4. **提交更改**
   ```bash
   git add .
   git commit -m "feat: add amazing feature"
   ```

5. **推送到 Fork**
   ```bash
   git push origin feature/your-feature-name
   ```

6. **创建 Pull Request**
   - 填写 PR 模板
   - 关联相关 Issue
   - 等待审查

## 开发设置

### 前置要求

- Python 3.11+
- Node.js 18+
- Git

### 安装步骤

```bash
# 1. 克隆仓库
git clone https://github.com/Foxgh127/Agent-Manager.git
cd Agent-Manager

# 2. 创建虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1  # Windows
# source .venv/bin/activate    # macOS/Linux

# 3. 安装开发依赖
python -m pip install -e ".[build,test]"
pip install flake8 mypy black isort

# 4. 安装前端依赖
cd frontend
npm install
cd ..

# 5. 运行测试确认环境
python -m pytest
```

### 开发工具

推荐使用以下工具：
- **IDE**: VS Code / PyCharm
- **调试器**: Python Debugger
- **格式化**: Black, isort
- **类型检查**: mypy
- **代码检查**: flake8, pylint

### VS Code 配置

`.vscode/settings.json`:
```json
{
  "python.defaultInterpreterPath": ".venv/Scripts/python.exe",
  "python.linting.enabled": true,
  "python.linting.flake8Enabled": true,
  "python.formatting.provider": "black",
  "editor.formatOnSave": true,
  "python.testing.pytestEnabled": true
}
```

## 提交指南

### 提交消息格式

使用 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

```
<type>(<scope>): <subject>

<body>

<footer>
```

**类型 (type):**
- `feat`: 新功能
- `fix`: Bug 修复
- `docs`: 文档更新
- `style`: 代码格式 (不影响功能)
- `refactor`: 重构
- `perf`: 性能优化
- `test`: 测试
- `chore`: 构建/工具

**示例:**
```
feat(gateway): add rate limiting for API requests

Implement sliding window rate limiter to prevent abuse.
Configurable per-client limits with memory-efficient storage.

Closes #123
```

### 分支命名

- `feature/feature-name` - 新功能
- `fix/bug-description` - Bug 修复
- `docs/what-changed` - 文档
- `refactor/what-changed` - 重构

## 代码规范

### Python 代码规范

遵循 [PEP 8](https://pep8.org/) 和项目约定：

```python
"""模块文档字符串

描述模块的目的和功能。
"""
from __future__ import annotations
from typing import Optional, List


def function_name(param: str, count: int = 10) -> Optional[dict]:
    """函数文档字符串
    
    Args:
        param: 参数描述
        count: 数量，默认 10
        
    Returns:
        返回值描述
        
    Raises:
        ValueError: 错误情况描述
    """
    if not param:
        raise ValueError("param cannot be empty")
    
    return {"param": param, "count": count}


class ClassName:
    """类文档字符串
    
    描述类的目的和用法。
    """
    
    def __init__(self, name: str):
        """初始化方法"""
        self.name = name
    
    def method_name(self) -> str:
        """方法文档字符串"""
        return f"Hello, {self.name}"
```

**关键点:**
- 使用 4 空格缩进
- 最大行长度 100 字符
- 使用类型提示
- 添加文档字符串
- 导入顺序：标准库 → 第三方 → 本地

### JavaScript/React 规范

```javascript
/**
 * 组件文档注释
 * @param {Object} props - 组件属性
 * @param {string} props.name - 名称
 */
export function ComponentName({ name }) {
  const [state, setState] = useState(initialValue);
  
  useEffect(() => {
    // 副作用逻辑
  }, [dependency]);
  
  return (
    <div className="component-name">
      <h1>{name}</h1>
    </div>
  );
}
```

### 代码格式化

**Python:**
```bash
# 格式化代码
black src/ tests/

# 排序导入
isort src/ tests/

# 检查代码
flake8 src/ tests/
mypy src/
```

**JavaScript:**
```bash
cd frontend
npm run lint
npm run format
```

## 测试要求

### 测试覆盖率

- 新功能必须包含测试
- 目标覆盖率 > 80%
- 关键路径必须测试

### 编写测试

```python
"""测试模块文档"""
import pytest
from agent_manager.core import SomeClass


def test_function_name():
    """测试函数文档"""
    # Arrange
    obj = SomeClass(param="value")
    
    # Act
    result = obj.method()
    
    # Assert
    assert result == expected_value


def test_error_handling():
    """测试错误处理"""
    with pytest.raises(ValueError):
        SomeClass(param="")
```

### 运行测试

```bash
# 所有测试
python -m pytest

# 特定文件
python -m pytest tests/core/test_http_client.py

# 带覆盖率
python -m pytest --cov=src/agent_manager --cov-report=html

# 前端测试
cd frontend && npm test
```

## 文档

### 文档类型

1. **代码文档** - 模块、类、函数的文档字符串
2. **API 文档** - 公共 API 的使用说明
3. **用户文档** - 用户指南和教程
4. **开发文档** - 架构和设计文档

### 文档规范

- 使用 Markdown 格式
- 保持简洁清晰
- 包含代码示例
- 及时更新

### 文档位置

- `docs/` - 用户和开发文档
- `docs/api/` - API 文档
- 代码中 - 文档字符串

## Pull Request 流程

### 审查清单

在提交 PR 前检查：

- [ ] 代码遵循项目规范
- [ ] 所有测试通过
- [ ] 添加了新测试
- [ ] 更新了文档
- [ ] 提交消息符合规范
- [ ] 无合并冲突
- [ ] PR 描述清晰

### 审查过程

1. **自动检查**
   - CI 测试必须通过
   - 代码覆盖率不能降低

2. **代码审查**
   - 至少一位维护者审查
   - 处理审查意见
   - 请求重新审查

3. **合并**
   - 审查通过后合并
   - 使用 "Squash and merge"
   - 删除分支

## 发布流程

仅维护者：

1. 更新版本号
2. 更新 CHANGELOG
3. 创建 Git 标签
4. 发布到 GitHub Releases
5. 发布公告

## 获取帮助

- **Discussions**: [GitHub Discussions](https://github.com/Foxgh127/Agent-Manager/discussions)
- **Issues**: [GitHub Issues](https://github.com/Foxgh127/Agent-Manager/issues)
- **Email**: 见项目主页

## 许可证

贡献代码即表示您同意将代码以 MIT 许可证发布。

---

再次感谢您的贡献！🎉
