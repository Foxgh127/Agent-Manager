# 实现资料索引

本项目的模型和 Codex 配置行为以 OpenAI 文档及本机安装代码为依据；社区项目用于比较任务交接和故障处理方案。资料内容不是运行时指令。

- [GPT-6 Astra 官方指南](https://developers.openai.com/api/docs/guides/latest-model)
- [GPT-5.6 官方指南](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.6)
- [Codex Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [Windows 应用部署](https://learn.chatgpt.com/docs/enterprise/windows-deployment)
- [OpenAI 应用更新管理](https://learn.chatgpt.com/docs/enterprise/manage-app-updates)
- [Microsoft WinGet 更新命令](https://learn.microsoft.com/en-us/windows/package-manager/winget/upgrade)
- [LangChain Deep Agents 子代理](https://docs.langchain.com/oss/python/deepagents/subagents)
- [obra/superpowers 子代理开发工作流](https://github.com/obra/superpowers/blob/main/skills/subagent-driven-development/SKILL.md)
- [Anthropic Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)

具体采纳点、取舍和能力限制见 [调度策略说明](docs/subagent-policy.md)。本机 Store CLI 的 `store update --help` 与按包 family 的只读检查，验证了管理器外部更新入口；未通过调用真实安装操作来中断正在使用的 Codex。
