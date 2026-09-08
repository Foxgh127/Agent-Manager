# Agent Manager

**仅供个人练手、学习与测试。项目代码、界面、调度提示词、文档及本次开发工作全部由 Codex 完成，由个人提出需求并使用。** 这是非官方实验项目，与 OpenAI、Anthropic 或任何中转站没有隶属关系；不作为生产服务或商业产品承诺。

面向 Windows 的本地 Codex 账号和 API 服务管理器。当前版本 **9.13.0**。

[下载 Windows EXE](https://github.com/Foxgh127/Agent-Manager/releases/latest) · [更新与发布](UPDATES.md) · [调度策略及资料来源](docs/subagent-policy.md)

## 本次完成

- Codex 的 API 登录名称跟随当前 API 卡片；模型选择器使用模型本名，内部路由标识保持可区分。
- 将 Agent Manager、Codex Desktop、Codex CLI 的版本放在同一维护区。Agent Manager 内置此仓库作为更新源，支持下载校验后替换并重启；Desktop 使用 Microsoft Store CLI 直接检查和更新。
- 普通关闭窗口或彻底退出时，先确认 Codex 已结束，再恢复原始配置，最后关闭服务。恢复冲突会保留管理器及恢复记录供重试。启用托盘时关闭窗口继续运行。
- 网站账号余额只采用对应 dashboard 的余额响应，正确接受零余额。读取失败时显示“待刷新”和上次读数，API Key 额度不再冒充账号余额。
- 改进 GPT-6 Astra 优先的子代理任务契约、能力与成本判断、生命周期回收及验证；按实际 GPT-5.6 Sol/Terra/Luna 模型生成相应工作要求。

## 使用

1. 从 Releases 下载 `AgentManager-版本号.exe`，放在自己可写的固定文件夹。
2. 在 Windows 上运行，确保 Microsoft Edge WebView2 Runtime 可用。
3. 按需导入自己的账号或配置自己的 API 服务，在调度中心保存并同步。
4. 在“设置 → 版本与维护”检查更新。有新版时点击“一键更新”；管理器会关闭 Codex、恢复原配置，再替换自身并重新启动。

桌面版外部更新需要 Windows 的 `store.exe`。系统不支持或输出不能确认时会如实显示检查失败或不可用，不会声称已是最新版。正在运行的 Codex 可能需要结束任务后才能完成 Store 安装。

“仅退出软件”是单独保留的高级例外：保留 Codex 与临时配置，但会停止管理器网关；依赖该网关的调用会中断。日常使用标题栏关闭、彻底退出或托盘即可。

## 从源码运行

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
npm ci --ignore-scripts --prefix gui
npm run build --prefix gui
python agent_manager_app.py
```

构建便携 EXE：

```powershell
./build_exe.ps1
```

## 发布新版本

提高 `gui/package.json`、`gui/package-lock.json`、`version_info.txt` 中的版本号，更新版本说明，然后提交到 `main`。GitHub Actions 自动验证、构建并发布 Release；客户端无需配置新地址。具体规则见 [UPDATES.md](UPDATES.md)。

账号和凭据保存在用户本机的数据目录。源码仓库不包含账号数据、Token、API Key、OAuth 文件、会话记录、个人验证截图或构建缓存。不要把个人配置和导出包提交到仓库。

## 验证范围

回归检查使用临时目录、合成数据及被替换的网络/进程接口；不会调用真实账号完成付费模型评测。调度提示词的检查证明策略被正确生成，不代表已测量模型效果提升。Windows 崩溃或强制结束进程时依赖持久恢复记录，无法承诺即时还原。

## 第三方说明

使用 Python、React、Vite、pywebview、PyInstaller、Pillow、pystray、tomlkit、lucide 等项目；各依赖遵循其自身许可。OpenAI、Codex、ChatGPT、Claude 及其标识属于相应权利人。参考资料仅用于实现研究，详见 [GITHUB_RESEARCH.md](GITHUB_RESEARCH.md) 和调度策略文档。
