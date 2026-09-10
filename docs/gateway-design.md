# 独立网关设计

1.1.0 继续运行 Python HTTP/SSE 网关，未引入 CPA 或 Sub2API sidecar。来源凭据、配置和模型路由使用本项目结构；不通过更换产品名称、降低模型或修改推理强度来声称兼容。

## 选择和会话

请求先确定公共或内部访问范围，再解析模型与允许使用的来源。专属模型别名、已有会话和可信响应续轮先固定身份；普通无绑定请求才参与池选择。

- `ordered` 保留来源顺序优先。
- `round_robin` 按模型维护身份游标，优先较少在途的来源。
- `quota_first` 先考虑在途负载，同等负载下参考 OAuth 周剩余额度。

选择与占用在同一个锁内完成。响应结束、失败或取消后释放占用。全局仍限制 8 个上游并发；这不是分布式调度器，也没有按账户套餐猜测硬并发上限。

同名共享模型的首来源为 API Provider 时，可以在已加入池、声明相同模型且可用的 Provider 中选择和安全回退。首来源为 OAuth 时使用 OAuth 池；不跨两种身份类别回退，不替换用户请求的模型和 reasoning effort。

会话键来自明确的 Session-Id、Session_id、X-Session-ID 或 Thread-Id，并与访问范围及模型一起哈希。持久记录只有哈希、来源 ID、身份指纹和到期时间，没有原始会话 ID、令牌或正文。最多 2048 条、闲置 1 小时；同池同凭据重启后可恢复绑定，凭据或许可范围变化会拒绝跨身份续轮。记录损坏或保存失败时返回错误，不静默重绑。

响应 ID 的逐响应绑定、冷却与轮询游标仍在进程内；未知多身份续轮、过期/驱逐记录不被当作可恢复原文。WebSocket 仍是串行 HTTP/SSE 桥接，未实现原生上游多路复用、warmup 或 steering。

## 失败与计量

明确的认证拒绝、限流或模型容量错误，只有在请求允许换号且尚未输出时才进入下一候选。文本、推理或工具增量输出后不重放；网络结果不确定也不盲目重复推理。Retry-After 的远端等待不再截短为 300 秒。

Responses/Chat 转换前的原始上游事件提供实际返回型号、服务档位和用量。输入大于 272K 的档位在每个请求完成时确定，不从每天累计 Token 反推；缺失的缓存读写、输入/输出字段仍标为未知。统计 schema 6 保留旧计数和计数代次，旧记录不虚构新证据。

## 源码参考与验证

阅读并固定参考 CPA 提交 `d1a024e9400bc65bd78ccd908945cf2eacc2835e`，采用其身份游标、模型分片、会话缓存和首输出前错误处理思路，自行实现本地模块：

- [选择器](https://github.com/router-for-me/CLIProxyAPI/blob/d1a024e9400bc65bd78ccd908945cf2eacc2835e/sdk/cliproxy/auth/selector.go)、[调度器](https://github.com/router-for-me/CLIProxyAPI/blob/d1a024e9400bc65bd78ccd908945cf2eacc2835e/sdk/cliproxy/auth/scheduler.go)
- [会话缓存](https://github.com/router-for-me/CLIProxyAPI/blob/d1a024e9400bc65bd78ccd908945cf2eacc2835e/sdk/cliproxy/auth/session_cache.go)
- [Codex 流式执行](https://github.com/router-for-me/CLIProxyAPI/blob/d1a024e9400bc65bd78ccd908945cf2eacc2835e/internal/runtime/executor/codex_executor_stream.go)、[响应转换](https://github.com/router-for-me/CLIProxyAPI/blob/d1a024e9400bc65bd78ccd908945cf2eacc2835e/internal/translator/codex/openai/chat-completions/codex_openai_response.go)

回归使用回环 HTTP 双上游、假凭据和临时状态，覆盖流式占用、身份隔离、持久会话、限流、工具参数、截断和计量迁移。没有以真实付费请求验证所有厂商线上行为，也不把兼容同名模型当作模型能力完全相同的证明。
