# 开发与发布

## 安装

使用 Python 3.11+ 虚拟环境与 Node.js 24，执行 README 中的 editable 安装和前端构建命令。源码导入统一以 `agent_manager` 为包根；不要把旧版散落模块加入 PYTHONPATH。

`pyproject.toml` 是依赖、测试入口和包信息的权威配置。`src/agent_manager/_version.py` 是产品版本与发布代次的唯一来源。`scripts/sync_version.py` 派生前端 manifest、版本常量、Windows 属性和资源版本文件。

## 验证

```powershell
python -m pytest
npm test --prefix frontend
```

测试按账号、配置、网关、会话、运行环境、更新等领域组织。必要的旧版本数据用于迁移回归；这不表示产品仍发布旧版本。

## 构建

```powershell
./scripts/build.ps1
```

`dist/` 放可分发文件，`artifacts/build/` 放可再生成的构建缓存。构建必须在前端成功并确认资源存在后进行；不停止用户正在使用的进程。标准文件被占用时输出明确的版本文件。

未发布的修复使用 `./scripts/build.ps1 -LocalBuild`，输出 `dist/AgentManager-local.exe` 和独立校验文件；产品版本不变，既有正式 EXE 与 SHA256.txt 不被替换。

## 发布

只有用户明确要求“发布”时才修改版本源、整理发行说明并发布。普通修复、构建和继续工作均不构成发布授权。推送和 PR 运行检查；正式发布必须手动触发工作流且 `publish=true`。需要由本机发布时，在获得同一发布授权并完成验证后执行：

版本号严格使用用户指定值，不自行递增。`docs/release-notes.md` 只写当次变化；发布脚本校验标题版本，拒绝缺失/重复的版本段落，且不会把旧版说明复制到新发行版。

```powershell
./scripts/publish.ps1 -Repository Foxgh127/Agent-Manager -Build -Publish -NotesFile docs/release-notes.md
```

脚本把目标仓库嵌入程序。先上传草稿，通过 Release ID 核对大小和 SHA-256，再公开；失败草稿可重试，已公开版本不能覆盖。发布使用 GitHub 自带 Token，无须额外分发凭据。版本重置采用 releaseEpoch=1；9.x 需手动跨代安装一次。

## 清理

`scripts/clean.ps1` 只清理明确的项目生成物和旧目录；正在使用或无法安全处理的路径会被保留并报告。用户 Codex 数据目录不在项目清理范围。配置备份保留由应用中的独立服务负责。

生成目录统一约定：`artifacts/build/` 为构建中间文件，`artifacts/publish/` 为发布暂存，`artifacts/checks/` 为本轮测试日志，`artifacts/reports/` 为本轮审查材料，`artifacts/preview/` 为隔离界面预览和测试下载。长期保留的结论整理到 `docs/`，不要把一次性诊断脚本留在项目根目录。

```powershell
./scripts/clean.ps1 -Preview                 # 先显示范围，不删除
./scripts/clean.ps1                          # 清理构建中间文件和缓存
./scripts/clean.ps1 -ReportsOnly             # 清理检查、审查和预览材料
./scripts/clean.ps1 -IncludeDependencies     # 另清前端依赖和可重新生成的资源
./scripts/clean.ps1 -LegacyRelease           # 另清已退役 release/，先结束其中程序
```

需要清理某个旧诊断目录时，使用 `python scripts/cleanup_project.py --reports-only --report-path artifacts/某个诊断目录` 预览，再加 `--apply`。自选路径必须是 `artifacts/` 下的明确文件或子目录，拒绝整个 `artifacts/`、源码和外部路径。所有删除都逐文件检查身份，不跟随符号链接或 Windows 重解析点；archives/reports 范围不会顺带清理源码字节码。

不要删除仍在使用的 `.venv` 或依赖目录后继续运行开发服务器。清理依赖后按 README 重新安装即可。
