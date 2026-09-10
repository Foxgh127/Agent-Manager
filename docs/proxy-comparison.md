# 本地网关与候选方案

核对日期：2026-09-10。本次只研究和比较，没有接入或替换任何代理引擎，也没有运行收费模型对照实验。

## 目前使用什么

Agent Manager 使用自己编写的 Python 网关，入口在 `src/agent_manager/gateway/service.py`，基于标准库 HTTP 服务。官方账号请求转发到 Codex 的 Responses 上游；API 来源转发到所选服务商。账号、模型、会话身份绑定和协议兼容由本项目维护。

它没有内嵌 Sub2API 或 CLIProxyAPI。`accounts/relay.py` 中对 Sub2API、New API 等站点的识别和余额读取，只是连接这些网站的账号接口。

当前支持 Responses、HTTP/SSE、工具与 Chat Completions 转换。客户端 WebSocket 使用串行 HTTP/SSE 桥接，并非原生上游 WebSocket；不支持其预热、多路复用和生成中 steering 等完整能力。这是现有实现的边界，不能据此声称和官方客户端性能相同。

## 方案比较

| 方案 | 主要用途与部署 | 对本项目的意义 |
| --- | --- | --- |
| 当前 Python 网关 | 随 Windows EXE 运行，与账号切换、会话及采样紧密结合 | 改动可控，但协议升级和兼容维护由本项目承担，WebSocket 仍有上述限制 |
| CLIProxyAPI（通常简称 CPA） | Go 代理，支持 Codex OAuth、多账号路由、工具和多种客户端协议；Windows 可直接运行发布文件 | 如果以后决定采用外部引擎，这是本地桌面场景优先验证的候选；仍需评估会话绑定、取消和更新生命周期。[项目](https://github.com/router-for-me/CLIProxyAPI)、[Windows 安装说明](https://help.router-for.me/introduction/quick-start#windows) |
| Sub2API | 面向订阅额度分配的完整服务平台，有账号、用户、分组、监控与计费；常规部署依赖 PostgreSQL 和 Redis | 更适合需要服务端和多用户管理的场景；对个人便携 EXE 会增加数据库、服务和迁移维护工作。[项目及部署说明](https://github.com/Wei-Shaw/sub2api) |
| LiteLLM | 聚合多家模型 API，提供路由、成本记录、日志及访问控制 | 如果重心转向多家正式 API 的统一管理，值得评估；不能把通用 API 兼容直接当作 Codex 订阅与全部原生会话功能的保证。[原作者项目](https://github.com/BerriAI/litellm) |
| Codex 官方直连 | 直接使用官方客户端和账号，或正式 API | 适合作为协议和效果比较的基线；账号池与本地自定义路由功能需另外考虑 |

这里按部署方式和本项目需求判断适配程度，没有以星标数量代替质量验证，也没有对候选引擎做性能排名。

## “降智”和“额度降低”怎样判断

目前查到的资料不足以证明“只要使用 Sub2API，就必然降低模型能力或官方额度”，也不足以保证 CPA 一定没有这些问题。需要区分请求实际变了、网关统计变了、与上游账户额度变了。

Sub2API 的 [0.2.0 发布说明](https://github.com/Wei-Shaw/sub2api/releases/tag/v0.2.0) 明确提供按模型映射 reasoning effort、超限时拒绝或降级等配置。因此应核对具体站点的映射和设置。其 [0.2.4 发布说明](https://github.com/Wei-Shaw/sub2api/releases/tag/v0.2.4) 还修复了额度未耗尽的 429 被错误触发退避的问题；这种网关状态错误不等于官方把额度下调。

从本项目的请求路径看，值得排查的是：实际模型和 effort 是否一致，指令、工具参数和推理上下文是否完整，长会话是否被裁剪，失败是否重复请求，以及切换账号后会话是否仍绑定正确。以上是可能的排查方向，不是已测出的故障。Sub2API 的 [README](https://github.com/Wei-Shaw/sub2api#nginx-reverse-proxy-note) 也明确指出 Nginx 默认丢弃带下划线的请求头会破坏粘性会话。

若后续决定评估替换，应固定客户端、账号、模型、effort 和任务集，对比任务通过率、工具调用完整性、缓存命中、输入/输出用量、取消后的重复消耗和延迟。仅凭回答长短、一次体验或第三方余额数字无法作出因果判断。

## 本次采样与美元显示采用的思路

参考 [CPA Usage Keeper](https://github.com/Willxup/cpa-usage-keeper) 的持久化用量、按模型和缓存分类统计方法，保留长期证据，区分累计用量和本周期可配对样本。该项目使用 SQLite；本项目沿用现有持久化存储，修复会话启动边界，避免为这个问题增加常驻服务。没有复制其代码。

[CLIProxyAPI README](https://github.com/router-for-me/CLIProxyAPI#usage-statistics) 说明自 6.10.0 起不再内置用量统计，改由独立采集服务完成。因此换成 CPA 也不自动解决持久化和额度估算问题。

美元口径采用已记录输入、缓存输入和输出，按模型参考价分别计算。它表达这些用量的 API 等值费用，不是订阅账号可提现的余额，也不是站点实际账单。只有连续、完整且同范围的配对样本足够时才估算窗口总额和剩余额；无法定价或统计缺失时显示相应状态。价格来源为 [OpenAI API 定价](https://developers.openai.com/api/docs/pricing)，具体限制见 [使用说明](usage.md)。
