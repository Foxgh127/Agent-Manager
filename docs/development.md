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

## 发布

修改版本源及 `docs/release-notes.md`，提交到 `main`。GitHub Actions 验证后执行：

```powershell
./scripts/publish.ps1 -Repository Foxgh127/Agent-Manager -Build -Publish -NotesFile docs/release-notes.md
```

脚本把目标仓库嵌入程序。先上传草稿，通过 Release ID 核对大小和 SHA-256，再公开；失败草稿可重试，已公开版本不能覆盖。发布使用 GitHub 自带 Token，无须额外分发凭据。版本重置采用 releaseEpoch=1；9.x 需手动跨代安装一次。

## 清理

`scripts/clean.ps1` 只清理明确的项目生成物和旧目录；正在使用或无法安全处理的路径会被保留并报告。用户 Codex 数据目录不在项目清理范围。配置备份保留由应用中的独立服务负责。

不要删除仍在使用的 `.venv` 或依赖目录后继续运行开发服务器。清理依赖后按 README 重新安装即可。
