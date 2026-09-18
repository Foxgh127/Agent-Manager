# 路由证据与安全边界

## 能确认什么

用量记录现在保留三层信息：请求要求的模型、管理器选择的路由模型，以及上游响应正文（优先）或明确允许的响应头中返回的模型。只有上游返回了可校验的模型名，才会标记为 `consistent` 或 `mismatch`；没有证据的旧记录保持 `unknown`。

Responses 响应中的 `model` 是实际生成该响应的模型 ID；管理器优先读取这个字段，再与配置路由比较。[官方 Responses API 参考](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)

如果响应提供 `system_fingerprint`（或受限的响应头等价字段），管理器只保存经过长度和字符校验的短指纹，并按来源和期望模型统计指纹变体。指纹变化表示服务端配置/基础设施证据发生变化，不能单独证明模型能力下降、账号被“降智”或触发了风控。

当前实现是被动观测：不会为了“指纹测试”自动发送额外问题、身份探测或对照请求。第三方项目例如 [api-model-spy](https://github.com/dabaibian/api-model-spy) 的黑盒探针可以帮助理解研究方向，但探针结果是启发式推断，并且会增加请求和用量；它不是官方模型身份证明，因此没有接入官方账号路径。

OpenAI 对 `system_fingerprint` 的说明是，它标识产生结果的模型权重、基础设施和配置组合，可用于复现性判断，但它本身不是能力评分。[OpenAI Cookbook 说明](https://cookbook.openai.com/examples/reproducible_outputs_with_the_seed_parameter)

## 官方账号与中转站隔离

`routingAudit` 随 `/api/usage` 和网关状态返回，明确记录：

- 官方 OAuth 请求直接进入 Codex Responses 上游；不会经过 Sub2API 账号/余额适配器。
- Sub2API、New API 等 `integrationKind` 只用于中转站账号的站点登录、Key 管理和额度读取；Provider 请求仍是独立的 OpenAI 兼容转发路径。
- 客户端身份头采用有限白名单；管理器不会生成伪造指纹，也不会在有输出后重放请求或跨身份续接会话。
- `safety_identifier` 等请求身份字段不会由管理器凭空生成或改写；如果调用方已经提供，则按原请求传递并由上游按其官方语义处理。
- 指纹和实际模型只写入有界用量元数据，不写入提示词、响应正文、令牌或原始会话标识。

这类边界审计不能保证上游服务的内部风控结果，但可以避免本地实现主动模拟第三方聚合器或通过额外探测制造异常流量。Codex 的模型目录缓存也可能按 `originator` 等身份维度变化，遇到目录陈旧时应刷新目录并查看实际响应模型，而不是仅凭界面推荐值判断。[Codex issue #33593](https://github.com/openai/codex/issues/33593)

## 跨电脑与权限检查

状态和配置都使用当前用户的 `CODEX_HOME` 及其 `agent-manager` 子目录。商店版 Codex 的受保护 `WindowsApps` 路径不直接执行；需要隔离账号或 Provider 环境时走包身份启动。管理员权限不会自动替换用户凭据或把状态写入管理员配置目录。

诊断时应优先检查 `/api/application/location`、`/api/codex-runtime` 和 `/api/health` 返回的解析路径、包身份和可用运行时。不要把某台电脑的绝对路径、`CODEX_HOME` 或环境变量值复制到另一台电脑。
