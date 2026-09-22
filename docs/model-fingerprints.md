# 被动模型指纹

1.3.3 在本地网关处理**正常推理响应**时采集指纹。不追加测试问题，不调用检测站点，不为检测发送额外的模型请求，也不下载远端指纹代码。

## 已接入的开源规则

- [modelprint / net-headerdna.js](https://github.com/unclecode/modelprint/blob/b220f28d2dbb5e8978d6f6b3baff254e543ef09d/probes/net-headerdna.js)，MIT，Unclecode / ItIsCuthNotCup：移植其响应头家族与响应 ID 前缀解析。删除原 `ctx.chat` 探测调用；仅使用现有响应。为保护隐私，只保存有限标签，省略原始响应头值、请求 ID 和任意 server 字符串。
- [LoongPort / passive.rs](https://github.com/SailingLoong/LoongPort/blob/91f67aaf79b101ab290b18e2fbfe76abecaed78c/src-tauri/src/relay/model_verification/passive.rs) 及协议 reducer，MIT，SailingLoong / Jason Young：借鉴有界协议事件归约，识别 Responses、Chat、Anthropic 与工具/推理签名形状。忽略回答中的自述身份；签名字段存在不等于验签成功。

许可证随包放在 `agent_manager/resources/licenses/`。这里移植的是纯解析部分，不运行这些项目的主动探针。

## 三层结果

1. **上游声明**：响应 `model` / 模型响应头，由服务商提供。历史 JSON 字段 `actualModel` 为兼容旧版本保留，含义是响应声明，不证明模型权重。
2. **协议与服务栈指纹**：`modelFingerprint` 包含规则版本、固定特征标签及其散列 `pfp_…`。Cloudflare/AWS/Envoy、协议类型或工具格式可能被多个模型共用，不能单独映射精确型号。
3. **模型候选**：如果当前 `system_fingerprint` 与近 30 天通过固定官方 Codex 端点正常调用时记录的响应指纹一致，则 `modelIdentity` 显示官方参考中的候选模型。参考只来自成功的官方直连响应，第三方声明不会训练索引。多模型共用指纹显示“歧义”；没有参考显示“未确认”。同指纹可以被复制，候选不是验真证书，没有伪造的置信度百分比。

索引只在读取用量快照时从既有记录构建，最多 1024 个指纹、每项最多显示 8 个候选；超限退回未知。旧记录没有被动证据时无法追溯补出真实型号。应用原生直连且未经过网关的流量只能显示原有日志信息。

## 模块边界

- `detection/model_fingerprint.py`：无网络、无凭据、无正文存储的纯协议解析。
- `detection/reference_index.py`：本机官方观察样本的有界候选关联。
- `usage/request_metadata.py`：将信号合入现有 JSON/SSE 计量元数据。
- `gateway/service.py`：转发前观察原始响应，保持响应字节与请求次数不变；用量快照补充候选。
- `frontend/src/components/UsageModelIdentity.jsx`：声明、候选、歧义与证据详情；导出采用相同筛选名称。

回归覆盖：正常 JSON/SSE、分块边界、响应头在转发前提取、零额外调用、敏感值不持久化、未知/歧义/参考过期、旧记录兼容以及筛选导出一致性。
