# Codex 显示名称与子代理调度策略

核对日期：2026-09-09。实现位于 `src/agent_manager/core/`；本次仅修改生成逻辑，没有写入真实 Codex 配置或重启 Codex。

## 显示名称与路由身份

过去 `build_codex_config()` 将共享网关的 provider `name` 固定写为“Agent Manager 聚合路由”；`build_synced_model_catalog()` 又把管理器内部的 `displayName` 直接写入 Codex `display_name`。当多来源具有同名模型时，内部标签会附加站点名称，所以底部模型名也带上站点。

现在共享网关的 `name` 取当前生成配置的主模型所属 API 卡片名称。网页登录账号优先从关联 `relayAccountId` 取卡片名称，避免 Provider 内部附带的 Key/线路文字。生成切换目标时，以目标主模型为准，即使界面仍显示旧账号。独立 API Provider 原有名称规则继续生效。官方账号和聚合池使用各自来源名；没有可定位来源时回退到 Agent Manager。

Codex catalog 的 `display_name` 使用原生模型 ID，例如 `gpt-6-astra`。来源仍可在模型说明中查看。管理器内部 `displayName`、`sourceId`、`key`、可见模型 slug、隐藏子代理别名及解析规则均保持原样。重名模型显示名称可以相同，路由 slug 仍唯一，已有会话的模型标识仍可解析。卡片重命名不会重新计算模型别名。

## 策略结构

调度策略由三部分生成：调用协议、所选策略正文、生命周期及有序路由。`OPTIMAL_ADAPTIVE_INSTRUCTIONS` 是自动模式的应用内置正文；`SUBAGENT_LIFECYCLE_SAFETY` 负责统一生命周期；`_render_managed_agent()` 给 worker 输出契约。Codex V2 mode hint 接收策略正文，完整的调用协议、生命周期和实际路由同时写入管理块。

| 阶段 | 执行要求 | 验收依据 |
| --- | --- | --- |
| 收益判断 | 只有独立子任务的收益超过交接、上下文及验证成本，且主代理有独立工作可继续时委派 | 小问题直接处理；有价值的独立调查实际调用对应 Agent |
| 难度判断 | 看歧义、影响范围、依赖深度、可逆性及验证负担，选择最低足够等级 | simple / normal / hard / expert 的既有顺序不变 |
| 任务契约 | 明确目标、验收标准、文件所有权、输入事实、约束、依赖、产物、证据、预计完成窗口 | 自包含交接；禁止子代理自行扩大范围或再次委派 |
| 并发 | simple/normal 每单元一个 worker；hard/expert 仅独立单元并行；共享文件和状态串行 | 不冲突、不重复，遵循运行时实际容量 |
| 失败分类 | Provider 启动前不可用才进入下一配置 fallback；本地容量不足先 drain；未知启动结果先核对 | 不把任务质量失败当服务不可用；不绕过容量限制 |
| 生命周期 | 每个 child 有状态、owner、最后证据、deadline；摘要交接也保留 ledger | 不把安静或一次超时当失败，不关闭正在运行的 child 释放槽位 |
| 收尾 | 先检查验收标准，再检查代码质量及集成风险；所有结果已消费并裁决后完成 | 不遗漏在运行或终态未读 child；工具已自动释放则不反复 close |

当前用户已配置的 `cam_simple_1`、`cam_normal_1`、`cam_hard_1`、`cam_expert_1` 以及对应模型和 effort 保持权威，策略文字不会在运行时重排它们。生成的 worker TOML 根据 `nativeModel` 只加入其实际模型对应的工作要求；隐藏网关别名不影响识别，`gpt-5.6` 按官方别名匹配 Sol。未识别的模型仅使用通用任务契约。

## 模型适配与证据

以下是面向本项目的工作分配建议，不是对模型能力的硬性限制。用户偏好 Astra 优先，所以新路由配置建议优先 Astra；现存路线和来源能力决定实际调用。

| 模型 | 本项目的任务契约重点 | 来源 |
| --- | --- | --- |
| GPT-6 Astra | 明确“何时必须委派”，保留持续执行范围，测试限于改动风险，避免小改动重复扩大测试 | [Astra 官方指南](https://developers.openai.com/api/docs/guides/latest-model) |
| GPT-5.6 Sol | 给出目标、领域上下文、硬约束、验收证据；留出实现判断空间 | [GPT-5.6 官方指南](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.6)、[Sol 模型页](https://developers.openai.com/api/docs/models/gpt-5.6-sol) |
| GPT-5.6 Terra | 适合边界明确的探索、以读取为主的分析、局部实施；交代集成边界 | [Codex Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)、[Terra 模型页](https://developers.openai.com/api/docs/models/gpt-5.6-terra) |
| GPT-5.6 Luna | 适合明确、重复、输入完整的单元；约定输出字段，遇到实质歧义返回主代理 | [Codex Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)、[Luna 模型页](https://developers.openai.com/api/docs/models/gpt-5.6-luna) |

实际查阅的官方资料支持：明确委派条件和结果格式、减少主线程噪声、对写密集并发保持谨慎；GPT-5.6 提示词应去除重复但保留成功标准。Astra API 支持 low/medium/high/xhigh/max，不支持 none；GPT-5.6 API 支持 none/low/medium/high/xhigh/max。Codex 的 Ultra 和 API effort 不能混用。[Astra 模型页](https://developers.openai.com/api/docs/models/gpt-6-astra)、[GPT-5.6 官方指南](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.6)

现有 effort 不自动提高。新默认沿用 simple=low、normal=medium、hard=high、expert=xhigh，并由已发现的具体来源能力和 Codex 兼容逻辑约束；不支持时保留来源默认。官方建议在代表性任务上比较原档位和较低档位，对最难任务再验证 max 的收益。这里没有运行收费模型评测，也没有声称某一档位有已测量的性能提升。代理服务转发的同名模型不能作为完整官方能力的证明。

## 社区原作者方案的取舍

已实际搜索并打开 [obra/superpowers 的 subagent-driven-development 原始文件](https://github.com/obra/superpowers/blob/main/skills/subagent-driven-development/SKILL.md?plain=1)。采纳它的任务材料包、进度 ledger、先验收规范再看代码质量、修复时复用原执行者上下文、只复查修复差异等思路。这里保留主代理裁决，不引入每个任务固定二次独立审查、固定五轮修复、默认嵌套或强制更换模型。这些工作流需要匹配任务成本，不能覆盖用户现有路由政策。

也搜索并打开了 [Anthropic 多代理研究系统原作者文章](https://www.anthropic.com/engineering/multi-agent-research-system)，但本次页面正文未可靠提取，因此没有将其性能数字或细节作为实现依据。社区文章不是本项目指令，也不用于证明 OpenAI 模型特性。

## 升级及验证边界

`_migrate_settings()` 会刷新应用内置策略文本，保留自定义模式 `parallel_first` 的用户文本、显式模型和 effort；Codex 原生模式继续不生成管理器策略和角色。管理器预览可审查生成的 TOML、catalog 和 AGENTS 内容，实际生效仍需用户既有“应用配置”流程。Codex 对已打开会话的缓存刷新行为不属于这次生成逻辑测试范围。

`test_codex_labels_policy_v913.py` 使用临时路径和假能力数据，检查生成配置、同名模型唯一解析、API 卡片切换与重命名、隐藏子代理标签、内置迁移、自定义保留、任务契约、源能力约束、原生策略及预览不写文件。提示词断言只证明契约被生成，不能代替真实模型任务评测。

## 补充交叉核对

主代理另读取了 [LangChain Deep Agents 子代理文档](https://docs.langchain.com/oss/python/deepagents/subagents) 和 [Anthropic Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)。前者支持把冗长工具输出隔离在 worker、传递明确系统说明与输出格式，并避免为单步工作支付委派成本；这些要求已体现在最小交接和简洁证据返回条款。后者建议从简单方案开始，并按工作流需要采用 orchestrator/worker 和可并行的独立单元；本项目据此保留主代理统一决策，不按固定代理数量扩张。此处是本项目对工作流资料的应用，不用于推断 GPT-6 或 5.6 的具体能力。
