# 🗂️ Agent Manager 项目结构详解

**更新日期**: 2026-09-21

---

## 📁 根目录文件（项目标准文件）

```
Agent-Manager/
├── README.md              # 项目主页，介绍功能、安装、使用
├── CONTRIBUTING.md        # 贡献指南，如何参与开发
├── LICENSE               # MIT 开源许可证
├── CHANGELOG.md          # 版本变更历史
├── QUICKSTART.md         # 5分钟快速开始指南
├── PROJECT_READY.md      # 发布准备清单
├── pyproject.toml        # Python 项目配置（依赖、版本等）
├── .gitignore           # Git 忽略规则（哪些文件不提交）
└── .gitattributes       # Git 属性配置
```

**这些文件的作用**:
- 标准的开源项目必备文件
- 让其他人了解项目、贡献代码、使用功能

---

## 📚 docs/ - 文档目录

```
docs/
├── api/                       # API 接口文档
│   └── README.md             # 所有公共 API 的使用说明
│
├── archive/                   # 历史归档
│   └── fixes-history/        # 之前的修复记录（已归档）
│
├── ARCHITECTURE.md            # 系统架构说明（新写的，详细）
├── CODE_STYLE.md             # 代码规范（Python & JS）
├── ROADMAP.md                # 产品路线图，未来计划
├── PROJECT_STATUS.md         # 当前项目状态
├── PROJECT_COMPLETION_REPORT.md  # 重构完成报告
├── PROJECT_FINAL_SUMMARY.md      # 最终总结
├── CLEANUP_REPORT.md         # 清理报告
│
├── architecture.md           # 原来的架构文档
├── development.md            # 开发指南
├── usage.md                 # 使用手册
├── gateway-design.md        # 网关设计说明
└── 其他技术文档...
```

**作用**: 给开发者和用户看的文档，解释如何使用、如何开发

---

## 💻 src/agent_manager/ - 主程序源代码

这是项目的核心代码目录：

```
src/agent_manager/
│
├── __init__.py              # 包初始化
├── __main__.py             # 程序入口（python -m agent_manager）
├── _version.py             # 版本号定义
├── paths.py                # 路径配置
│
├── core/                   # 核心功能模块 ⭐
│   ├── __init__.py
│   │
│   ├── app_server/        # ✅ App Server 模块（已模块化）
│   │   ├── __init__.py
│   │   ├── client.py      # HTTP 请求核心
│   │   ├── thread_utils.py # 线程工具
│   │   ├── health.py      # 健康检查
│   │   └── management.py  # 线程管理
│   │
│   ├── runtime/           # ⚠️ Runtime 模块（需要重构）
│   │   └── __init__.py    # 目前依赖 runtime.py.bak
│   │
│   ├── switching/         # ⚠️ Switching 模块（需要重构）
│   │   └── __init__.py    # 目前依赖 switching.py.bak
│   │
│   ├── translations/      # ✅ 国际化翻译文件
│   │   ├── __init__.py
│   │   ├── zh-CN.json    # 中文
│   │   └── en-US.json    # 英文
│   │
│   ├── http_client.py     # ✅ 统一 HTTP 客户端（新）
│   ├── rate_limiter.py    # ✅ 速率限制器（新）
│   ├── url_validator.py   # ✅ URL 验证器（新）
│   ├── process_utils.py   # ✅ 进程工具（新）
│   ├── i18n.py           # ✅ 国际化系统（新）
│   │
│   ├── runtime.py.bak    # ⚠️ 临时备份，1884行，需要拆分
│   ├── switching.py.bak  # ⚠️ 临时备份，1075行，需要拆分
│   │
│   └── 其他核心模块...
│       ├── auth.py           # 认证
│       ├── agents.py         # 代理管理
│       ├── catalog.py        # 模型目录
│       ├── configuration.py  # 配置
│       └── ...
│
├── accounts/              # 账号管理
│   ├── __init__.py
│   ├── import_formats.py  # 导入格式处理
│   ├── portability.py     # 账号导出
│   ├── reauthentication.py # 重新认证
│   ├── relay.py          # 中转站账号
│   └── subscription.py   # 订阅信息
│
├── gateway/              # API 网关（本地代理）
│   ├── __init__.py
│   ├── service.py        # 网关服务
│   ├── scheduling.py     # 请求调度
│   └── websocket.py      # WebSocket 支持
│
├── sessions/             # 会话管理
│   ├── __init__.py
│   ├── history.py        # 会话历史
│   ├── visibility.py     # 可见性控制
│   ├── repair.py         # 会话修复
│   └── ...
│
├── storage/              # 数据存储
│   ├── __init__.py
│   ├── coordinator.py    # 存储协调器
│   ├── recovery.py       # 数据恢复
│   └── snapshot_service.py # 快照服务
│
├── platform/             # 平台适配（Windows相关）
│   ├── __init__.py
│   ├── dlls.py          # DLL 处理
│   ├── notifications.py  # 系统通知
│   ├── paths.py         # 路径处理
│   ├── webview.py       # WebView 窗口
│   └── ...
│
├── application/          # 应用层
│   ├── __init__.py
│   ├── http.py          # HTTP 服务器
│   ├── server.py        # WebSocket 服务器
│   ├── lifecycle.py     # 生命周期管理
│   └── ...
│
├── integrations/         # 第三方集成
│   ├── claude.py        # Claude 集成
│   ├── radar.py         # Radar 服务
│   └── ...
│
├── updates/              # 更新系统
│   ├── service.py       # 更新服务
│   ├── installer.py     # 安装器
│   └── ...
│
├── usage/                # 用量统计
│   ├── estimation.py    # 用量估算
│   ├── pricing.py       # 价格计算
│   └── ...
│
└── resources/            # 资源文件
    ├── ui/              # 前端资源（打包后）
    ├── app-update-source.json
    └── version.json
```

---

## 🧪 tests/ - 测试代码

```
tests/
├── __init__.py
│
├── core/                 # 核心模块测试
│   ├── test_app_server.py      # ✅ 6个测试
│   ├── test_http_client.py     # ✅ 6个测试
│   ├── test_i18n.py           # ✅ 3个测试
│   ├── test_process_utils.py   # ✅ 5个测试
│   └── test_url_validator.py   # ✅ 7个测试
│
├── security/             # 安全测试
│   ├── test_auth_hardening.py  # ✅ 4个测试
│   └── test_rate_limiter.py    # ✅ 9个测试
│
├── accounts/            # 账号测试
├── gateway/             # 网关测试
├── sessions/            # 会话测试
└── ...
```

**作用**: 验证代码正确性，防止 bug

---

## 🎨 frontend/ - React 前端

```
frontend/
├── src/                 # React 源代码
│   ├── App.jsx         # 主组件
│   ├── components/     # UI 组件
│   └── ...
├── public/             # 静态资源
├── dist/               # 构建产物
├── package.json        # Node.js 配置
└── vite.config.js      # Vite 配置
```

**作用**: 用户看到的界面（网页）

---

## 🔧 其他目录

```
.github/workflows/       # CI/CD 自动化（GitHub Actions）
scripts/                # 工具脚本（构建、发布等）
packaging/              # 打包相关（Windows EXE）
artifacts/              # 构建产物（不提交到 git）
dist/                   # 最终安装包输出
```

---

## ⚠️ 当前的问题

### 问题 1: 依赖备份文件（不专业）

```python
# src/agent_manager/core/runtime/__init__.py
_runtime_backup = Path(__file__).parent.parent / "runtime.py.bak"
with open(_runtime_backup, 'r', encoding='utf-8') as f:
    code = f.read()
exec(code, globals())  # 动态执行备份文件的代码
```

**为什么这样不好**:
- 依赖临时的 .bak 文件
- 使用 `exec()` 动态执行代码（危险、难调试）
- 不是真正的模块化

### 问题 2: 文件太大

- `runtime.py.bak`: **1884行**, 42个函数 - 太大，难维护
- `switching.py.bak`: **1075行**, 29个函数 - 太大，难维护

---

## ✅ 应该怎么做

### 真正的模块化方案

#### Runtime 模块拆分（8个子模块）:

```
src/agent_manager/core/runtime/
├── __init__.py          # 统一导出接口
├── path_utils.py        # 路径处理（4个函数）
├── node_discovery.py    # Node.js 发现（4个函数）
├── cli_management.py    # CLI 管理（5个函数）
├── download.py          # 下载安装（6个函数）
├── desktop_integration.py # Desktop 集成（5个函数）
├── launcher.py          # 启动相关（9个函数）
├── status.py           # 状态和部署（6个函数）
└── executor.py         # 执行器（1个函数）
```

#### Switching 模块拆分（5个子模块）:

```
src/agent_manager/core/switching/
├── __init__.py               # 统一导出接口
├── account_switch.py         # 账号切换核心（4个函数）
├── config_validation.py      # 配置验证（5个函数）
├── session_repair.py         # 会话修复（3个函数）
├── official_integration.py   # 官方账号集成（4个函数）
└── provider_utils.py         # 提供商工具（13个函数）
```

**这样做的好处**:
- 每个文件职责单一，容易理解
- 不依赖备份文件
- 容易测试、容易维护
- 符合专业项目标准

---

## 📊 总结

| 模块 | 当前状态 | 应该是什么 |
|------|---------|-----------|
| app_server | ✅ 已模块化（4个文件） | 保持 |
| runtime | ⚠️ 依赖 .bak 文件 | 拆分成 8 个模块 |
| switching | ⚠️ 依赖 .bak 文件 | 拆分成 5 个模块 |
| 其他工具 | ✅ 独立文件 | 保持 |

---

你说得对，我们不应该依赖备份文件。让我彻底重构这两个模块！
