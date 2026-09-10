# 1.1.4 验证记录

验证日期：2026-09-10。

- 完整后端回归：1906 passed、5 skipped、266 subtests passed；最后补充的复制失败恢复通过 27 项移动专项及 19 项子用例。
- 前端：72 项测试通过，生产构建通过；验证中文目标路径、取消、重复点击防护、移动失败反馈及三项状态说明的移除。
- 使用真实 Windows PowerShell 5 验证中文、空格、引号、方括号路径；验证清理凭据、文件锁重试、回滚保护及重解析点拒绝。
- 使用临时文件验证跨盘复制、启动确认后的旧文件删除、目标冲突、复制失败恢复，以及绝对 CODEX_HOME 保留。
- 首次云端矩阵暴露 WScript 快捷方式保存依赖系统默认编码；本机使用非当前代码页字符复现路径变为问号。改用 [IShellLinkW](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nn-shobjidl_core-ishelllinkw) 与 [IPersistFile](https://learn.microsoft.com/en-us/windows/win32/api/objidl/nf-objidl-ipersistfile-save) 的 Unicode 路径读写，并增加相应回归。
- Windows Desktop Known Folder 读取和临时重定向桌面上的原生快捷方式验证覆盖重复创建、更新目标及同名其他快捷方式保护。
- 实际管理器进程启动使用隔离替身，未移动正在运行的应用或修改真实桌面快捷方式。
- 安装包检查资源、模块、版本与发布代次，并在中文及空格目录从不同工作目录验证帮助入口。

清理仅处理本应用已记录并验证的文件；未知历史文件、仍需恢复的原件和持续被占用的文件会保留。旧版更新助手已产生路径乱码时，需先手动换用本版一次。

最终提交的云端矩阵见 [GitHub Actions](https://github.com/Foxgh127/Agent-Manager/actions/workflows/release.yml)。
