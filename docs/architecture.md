# 模块与设计依据

## 目录约定

采用 [PyPA 的 src layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)：可导入源码只放在 `src/agent_manager`，测试、脚本、文档和构建产物各自独立。这样可避免从任意工作目录意外导入散落的同名模块。参考 [BeeWare Briefcase](https://github.com/beeware/briefcase) 对原生应用源码、平台支持与构建命令的划分，但本项目继续使用既有 PyInstaller 和 React 技术栈。

资源定位遵循 [PyInstaller runtime information](https://pyinstaller.org/en/stable/runtime-information.html)：区分包资源、解包目录和用户数据，不从安装目录猜测账号配置。运行时路径集中于 `paths.py`。

## 后端责任

| 包 | 责任 |
| --- | --- |
| application | HTTP、窗口、OAuth 交互、后台任务及退出交接 |
| core | 稳定的服务接口与共享锁；内部文件按配置编辑、凭据、账号导入、刷新、模型目录、渲染、事务、运行环境和历史等主题拆分 |
| accounts | 网页站点适配、凭据格式与账号迁移 |
| config | 配置备份、恢复、保留及引用保护 |
| gateway | 上游路由、Responses/Chat 适配与 WebSocket |
| sessions | 会话索引、来源可见性及可撤销恢复 |
| updates | 发布发现、下载校验、安装助手与系统更新 |
| platform | Windows 能力探测、文件属性、DLL 与窗口主题 |
| integrations / usage | 保留的其他工作区和用量统计 |

原先的大核心和应用文件已拆成主题模块。`core/__init__.py` 和 `application/__init__.py` 负责明确组合公共接口与共享运行状态，跨模块调用显式通过该接口；没有使用 `exec` 拼接源码或创建旧路径兼容壳。这样保留了已有事务和依赖注入边界，同时避免在目录迁移中产生第二套配置锁或缓存。

必要的旧数据读取仍保留。例如旧账号导出格式、原始会话及配置迁移不能简单当作死代码删除。清理针对重复产物、无效构建逻辑和已经失去意义的文本结构测试；行为回归继续保留。

## 会话与账户隔离

原始历史不可被为了适配某个 Provider 而改写。网关仅对可验证完整的无状态请求副本适配不兼容加密推理，并限制到同一身份和模型的一次重试。服务端游标、压缩密文或不完整工具链不自动迁移；已输出请求不重放。

站点验证页和身份过期分开处理。同账号网页登录窗口允许正常读取，但不导出挑战 cookie，也不猜测其他身份。对 FastAI 的观察证实其后台请求会被验证页拒绝；Sub2API 对周期限额的上游定义为大于零才启用，参考 [Group limit methods](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/service/group.go)。

## 备份和版本

配置备份以内容指纹去重。正常自动快照和恢复前原件共享最近三份不同内容的上限；手动、外部或未解决引用有明确分类和保护原因。删除校验路径、内容和文件身份。

产品版本和发布代次来自 `_version.py`，不与用户数据 schema 混用。1.0.0 的代次防止旧缓存或旧 EXE 参与新的更新选择。旧9.x程序不理解新代次，所以跨代首次采用明确的手动安装，不伪造一个更大的版本号。

模型和调度的官方/社区依据见 [subagent-policy.md](subagent-policy.md)。这些资料用于设计比较，不是运行时指令。
