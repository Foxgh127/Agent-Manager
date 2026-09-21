# Agent Manager

<div align="center">
  <img src="frontend/public/app-icon.png" width="128" alt="Agent Manager Logo">
  
  <p><strong>现代化的 AI 账号和 API 管理工作台</strong></p>
  
  [![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
  [![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
  [![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)](https://github.com/Foxgh127/Agent-Manager/actions)
  [![Version](https://img.shields.io/badge/version-1.3.0-orange.svg)](https://github.com/Foxgh127/Agent-Manager/releases)
  
  [English](README.md) | [简体中文](README_zh-CN.md)
</div>

---

## ✨ 特性

- 🔐 **多账号管理** - 统一管理 Claude、ChatGPT 等多个 AI 服务账号
- 🔄 **智能切换** - 快速在不同账号和 API 密钥之间切换
- 📊 **用量监控** - 实时监控 API 使用情况和配额
- 🌐 **本地网关** - 内置安全的本地 API 代理网关
- 💾 **配置备份** - 自动备份和恢复配置文件
- 🔔 **智能通知** - 配额预警和服务状态通知
- 🌍 **国际化** - 支持中英文界面

## 📦 安装

### 从发布包安装 (推荐)

1. 从 [Releases](https://github.com/Foxgh127/Agent-Manager/releases/latest) 下载最新版本
2. 运行安装程序或解压便携版
3. 启动 Agent Manager

### 从源码构建

**前置要求:**
- Python 3.11 或更高版本
- Node.js 18 或更高版本
- Windows 10/11 (主要支持平台)

**步骤:**

```bash
# 克隆仓库
git clone https://github.com/Foxgh127/Agent-Manager.git
cd Agent-Manager

# 创建虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1  # Windows
# source .venv/bin/activate    # macOS/Linux

# 安装依赖
python -m pip install -e ".[build,test]"
cd frontend && npm install && cd ..

# 构建前端
cd frontend && npm run build && cd ..

# 运行
python -m agent_manager
```

## 🚀 快速开始

### 1. 启动应用

```bash
python -m agent_manager
```

### 2. 添加账号

1. 点击 "添加账号"
2. 选择服务类型 (Claude/ChatGPT/API Key)
3. 输入凭据
4. 保存

### 3. 配置路由

1. 进入 "调度中心"
2. 选择默认模型
3. 配置子代理策略
4. 保存并同步

### 4. 开始使用

- 通过本地网关访问: `http://localhost:8000`
- 使用配置的账号和模型
- 实时监控用量和配额

## 📚 文档

- [用户手册](docs/usage.md) - 完整的使用说明
- [API 文档](docs/api/README.md) - API 接口文档
- [架构设计](docs/architecture.md) - 系统架构说明
- [开发指南](docs/development.md) - 开发和贡献指南
- [故障排除](docs/troubleshooting.md) - 常见问题解决

## 🏗️ 项目结构

```
Agent-Manager/
├── src/agent_manager/        # 主程序源代码
│   ├── core/                 # 核心功能模块
│   │   ├── app_server/       # App Server 客户端
│   │   ├── runtime/          # 运行时管理
│   │   ├── switching/        # 账号切换
│   │   ├── http_client.py    # HTTP 客户端
│   │   ├── rate_limiter.py   # 速率限制
│   │   └── i18n.py           # 国际化
│   ├── accounts/             # 账号管理
│   ├── gateway/              # API 网关
│   ├── sessions/             # 会话管理
│   ├── storage/              # 存储服务
│   └── platform/             # 平台适配
├── frontend/                 # React 前端
├── tests/                    # 测试套件
├── docs/                     # 文档
└── scripts/                  # 工具脚本
```

详细结构请查看 [架构文档](docs/architecture.md)

## 🧪 测试

```bash
# 运行所有测试
python -m pytest

# 运行特定模块测试
python -m pytest tests/core/

# 运行前端测试
cd frontend && npm test

# 生成覆盖率报告
python -m pytest --cov=src/agent_manager --cov-report=html
```

**当前测试状态**: 40/40 测试通过 ✓

## 🔒 安全性

本项目实现了多层安全防护：

- ✅ **速率限制** - 防止 API 滥用
- ✅ **JWT 验证** - 安全的令牌管理
- ✅ **URL 验证** - SSRF 防护
- ✅ **输入验证** - 防止注入攻击
- ✅ **加密存储** - Windows DPAPI 保护敏感数据

查看 [安全文档](docs/security.md) 了解详情。

## 🤝 贡献

欢迎贡献！请查看 [贡献指南](CONTRIBUTING.md)。

### 贡献者

感谢所有贡献者！

<a href="https://github.com/Foxgh127/Agent-Manager/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=Foxgh127/Agent-Manager" />
</a>

## 📄 许可证

本项目采用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件了解详情。

## 🙏 致谢

- [Claude](https://claude.ai/) - AI 辅助开发
- [React](https://react.dev/) - 前端框架
- [FastAPI](https://fastapi.tiangolo.com/) - 启发的 API 设计
- 所有开源项目的贡献者

## 📮 联系方式

- **Issues**: [GitHub Issues](https://github.com/Foxgh127/Agent-Manager/issues)
- **Discussions**: [GitHub Discussions](https://github.com/Foxgh127/Agent-Manager/discussions)

## 🗺️ 路线图

查看 [路线图](docs/ROADMAP.md) 了解未来计划。

- [x] 多账号管理
- [x] 本地网关
- [x] 配置备份
- [x] 国际化支持
- [x] 模块化重构
- [ ] macOS 支持
- [ ] Linux 支持
- [ ] 插件系统
- [ ] 云同步

## ⭐ Star 历史

[![Star History Chart](https://api.star-history.com/svg?repos=Foxgh127/Agent-Manager&type=Date)](https://star-history.com/#Foxgh127/Agent-Manager&Date)

---

<div align="center">
  Made with ❤️ by the Agent Manager team
  
  如果这个项目对您有帮助，请考虑给它一个 ⭐️
</div>
