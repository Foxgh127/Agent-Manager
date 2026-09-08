# 更新与发布

客户端内置 `Foxgh127/Agent-Manager` 的公开 GitHub Releases，无需用户填写发布源。设置中的三个版本卡片并排显示；Agent Manager 有新版时可一键下载、校验、恢复 Codex 配置、替换自身并重启。

## 日常发布

1. 将 `gui/package.json`、`gui/package-lock.json` 的根版本和 packages 根版本、`version_info.txt` 的数字及文本版本同步提高为 `x.y.z`。同时更新首页/PORTABLE 的展示版本与 `docs/release-notes.md`。
2. 将代码推送至本仓库 `main`（也可在 GitHub 上传源码修改）。
3. `Build and publish Agent Manager` 工作流先检查该版本是否存在，再运行回归和构建。首次发布此版本时生成 `vX.Y.Z` Release、`AgentManager-X.Y.Z.exe`、`SHA256.txt` 和 manifest。
4. 已存在的版本不会被覆盖。修复后提高版本号再次推送即可。

流程使用 GitHub 内置的 `GITHUB_TOKEN`；不需要额外配置账号 Token 或更新服务器。构建自动嵌入当前仓库名，因此 fork 后在自己的仓库构建也会指向自己的 Releases。

## 本机构建与发布

```powershell
./publish_release.ps1 -Repository Foxgh127/Agent-Manager -Build
./publish_release.ps1 -Repository Foxgh127/Agent-Manager -Publish -NotesFile ./docs/release-notes.md
```

先构建/校验，发布脚本上传草稿并核对 GitHub 返回的大小和 digest，校验成功后公开。脚本不会覆盖已发布版本。也可手动向同一仓库 Release 上传对应名称的 EXE；Release 标签与文件名中的版本必须一致，GitHub API 必须提供 SHA-256 digest。

## 更新行为

- 只有成功检查到更高版本才能下载；失败/离线不会伪装成最新版。
- 下载完成和安装前分别验证大小及 SHA-256。后台助手等待同一管理器实例确认已恢复配置并完全退出，再原子替换文件。
- 退出恢复失败、文件被改动或助手无法独立启动时停止更新并保留原程序。
- 自动安装适用于 Windows EXE；源码运行时提供下载。不要把程序放在自己无写权限的目录。
- SHA-256 验证传输完整性；项目发布的 EXE 尚无发布者数字签名。

Codex Desktop 使用系统 Microsoft Store CLI 和实际安装的 OpenAI 包 family 直接检查/安装，不把 Codex CLI 的 winget 包当成桌面版，也不自动关闭正在进行任务的 Desktop 来测试更新。
