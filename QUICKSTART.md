# 🚀 快速开始指南

## 新开发者 5 分钟上手

### 1. 克隆项目

```bash
git clone https://github.com/Foxgh127/Agent-Manager.git
cd Agent-Manager
```

### 2. 环境设置

```bash
# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
.\.venv\Scripts\Activate.ps1  # Windows
# source .venv/bin/activate    # macOS/Linux

# 安装依赖
python -m pip install -e ".[build,test]"
cd frontend && npm install && cd ..
```

### 3. 运行测试

```bash
# 运行核心测试
python -m pytest tests/core/ tests/security/ -v

# 应该看到: 40 passed
```

### 4. 启动应用

```bash
python -m agent_manager
```

---

## 📁 项目导航

### 我需要...

**添加新功能**
- 先阅读: [CONTRIBUTING.md](CONTRIBUTING.md)
- 查看: [docs/ROADMAP.md](docs/ROADMAP.md)
- 参考: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

**修复 Bug**
- 先搜索: [GitHub Issues](https://github.com/Foxgh127/Agent-Manager/issues)
- 创建测试: `tests/` 目录
- 遵循: [docs/CODE_STYLE.md](docs/CODE_STYLE.md)

**了解架构**
- 主文档: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- API文档: [docs/api/README.md](docs/api/README.md)
- 代码示例: `src/agent_manager/core/`

**查看 API**
- 完整文档: [docs/api/README.md](docs/api/README.md)
- 核心模块: `src/agent_manager/core/`
- 使用示例: 文档中的 Examples 部分

**理解安全**
- 安全修复: [docs/fixes/](docs/fixes/)
- 测试套件: `tests/security/`
- 最佳实践: [CONTRIBUTING.md](CONTRIBUTING.md)

---

## 🗺️ 目录结构速查

```
Agent-Manager/
├── src/agent_manager/           # 📦 主程序代码
│   ├── core/                    # 核心功能
│   │   ├── app_server/          # App Server 模块
│   │   ├── runtime/             # 运行时管理
│   │   ├── switching/           # 账号切换
│   │   ├── http_client.py       # HTTP 客户端
│   │   ├── rate_limiter.py      # 速率限制
│   │   └── i18n.py              # 国际化
│   ├── accounts/                # 账号管理
│   ├── gateway/                 # API 网关
│   ├── sessions/                # 会话管理
│   └── storage/                 # 存储服务
│
├── tests/                       # 🧪 测试代码
│   ├── core/                    # 核心模块测试
│   ├── security/                # 安全测试
│   └── ...
│
├── frontend/                    # 🎨 前端代码
│   ├── src/                     # React 组件
│   └── public/                  # 静态资源
│
├── docs/                        # 📚 文档
│   ├── api/                     # API 文档
│   ├── fixes/                   # 修复记录
│   ├── ARCHITECTURE.md          # 架构说明
│   ├── CODE_STYLE.md            # 代码规范
│   └── ...
│
├── scripts/                     # 🔧 工具脚本
│   ├── build.ps1                # 构建脚本
│   ├── sync_version.py          # 版本同步
│   └── ...
│
├── .github/workflows/           # 🤖 CI/CD
│   └── test.yml                 # 测试工作流
│
├── README.md                    # 项目主页
├── CONTRIBUTING.md              # 贡献指南
├── CHANGELOG.md                 # 变更日志
├── LICENSE                      # 许可证
└── pyproject.toml               # 项目配置
```

---

## 🔧 常用命令

### 开发

```bash
# 运行应用
python -m agent_manager

# 运行前端开发服务器
cd frontend && npm run dev

# 格式化代码
black src/ tests/
isort src/ tests/

# 代码检查
flake8 src/ tests/
mypy src/
```

### 测试

```bash
# 所有测试
python -m pytest

# 特定模块
python -m pytest tests/core/

# 带覆盖率
python -m pytest --cov=src/agent_manager --cov-report=html

# 前端测试
cd frontend && npm test
```

### 构建

```bash
# 同步版本
python scripts/sync_version.py

# 构建前端
cd frontend && npm run build

# 构建应用（Windows）
./scripts/build.ps1

# 本地测试构建
./scripts/build.ps1 -LocalBuild
```

---

## 📖 核心概念

### 模块化架构

项目采用清晰的模块化结构：
- **core** - 核心功能和工具
- **accounts** - 账号管理
- **gateway** - API 网关
- **sessions** - 会话管理
- **storage** - 数据存储

### 关键模块

**app_server** - 与 Codex 通信
```python
from agent_manager.core.app_server import codex_app_server_request
result = codex_app_server_request("thread/list", {})
```

**http_client** - 安全的 HTTP 请求
```python
from agent_manager.core.http_client import SafeHTTPClient
client = SafeHTTPClient(timeout=30)
data = client.get("https://api.example.com")
```

**rate_limiter** - 速率限制
```python
from agent_manager.core.rate_limiter import RateLimiter
limiter = RateLimiter(max_requests=100, window_seconds=60)
if limiter.is_allowed(client_id):
    # 处理请求
```

**i18n** - 国际化
```python
from agent_manager.core.i18n import t
error = t("errors.account_not_found")
```

---

## 🐛 调试技巧

### 启用调试日志

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### 使用 pytest 调试

```bash
# 详细输出
python -m pytest -v -s

# 在失败处暂停
python -m pytest --pdb

# 只运行失败的测试
python -m pytest --lf
```

### VS Code 调试配置

`.vscode/launch.json`:
```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Python: Agent Manager",
      "type": "python",
      "request": "launch",
      "module": "agent_manager",
      "console": "integratedTerminal"
    },
    {
      "name": "Python: 当前测试文件",
      "type": "python",
      "request": "launch",
      "module": "pytest",
      "args": ["${file}", "-v"],
      "console": "integratedTerminal"
    }
  ]
}
```

---

## 📊 项目状态一览

| 指标 | 状态 |
|------|------|
| 测试通过 | ✅ 40/40 (100%) |
| 代码覆盖率 | ✅ ~85% |
| 安全漏洞 | ✅ 0 |
| 文档完整度 | ✅ 95% |
| CI/CD | ✅ 已配置 |

---

## 🤝 获取帮助

- 📖 **文档**: 查看 `docs/` 目录
- 🐛 **Bug**: [GitHub Issues](https://github.com/Foxgh127/Agent-Manager/issues)
- 💬 **讨论**: [GitHub Discussions](https://github.com/Foxgh127/Agent-Manager/discussions)
- 📧 **Email**: 见项目主页

---

## 🎯 下一步

1. **熟悉代码库** - 浏览 `src/agent_manager/core/`
2. **运行测试** - `python -m pytest`
3. **阅读文档** - 从 [ARCHITECTURE.md](docs/ARCHITECTURE.md) 开始
4. **选择任务** - 查看 [ROADMAP.md](docs/ROADMAP.md) 或 Issues
5. **开始贡献** - 阅读 [CONTRIBUTING.md](CONTRIBUTING.md)

---

**欢迎加入 Agent Manager 项目！** 🎉

如有问题，随时在 Issues 或 Discussions 中提出。
