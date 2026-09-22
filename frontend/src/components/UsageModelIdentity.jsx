import { usageModelRouting } from "../usageRouting.js";
import "./UsageModelIdentity.css";

const PROTOCOL_LABELS = {
  openai_responses: "OpenAI Responses",
  openai_chat: "OpenAI Chat Completions",
  anthropic_messages: "Anthropic Messages",
  gemini: "Gemini",
};

const SOURCES = [
  ["modelprint", "https://github.com/unclecode/modelprint/blob/b220f28d2dbb5e8978d6f6b3baff254e543ef09d/probes/net-headerdna.js"],
  ["LoongPort", "https://github.com/SailingLoong/LoongPort/blob/91f67aaf79b101ab290b18e2fbfe76abecaed78c/src-tauri/src/relay/model_verification/passive.rs"],
];

export default function UsageModelIdentity({ item }) {
  const model = usageModelRouting(item);
  const ambiguous = model.identityStatus === "ambiguous";
  return <div className="usage-model-cell usage-model-identity">
    <code className="usage-model-name">{model.displayModel}</code>
    <span className={`usage-model-evidence ${model.identityStatus}`}>{model.identityLabel}</span>
    {model.mismatch && <span className="model-routing-issue" title="上游声明的模型名与配置路由不一致">路由：{model.expectedModel || "未知"}</span>}
    <details className="usage-model-details">
      <summary>{ambiguous ? `指纹详情 · ${model.identityCandidates.length} 个候选` : "指纹详情"}</summary>
      <div className="usage-model-detail-body">
        {ambiguous && <p className="usage-model-ambiguous">该指纹对应多个模型，无法唯一判断型号。</p>}
        {model.identityStatus === "candidate" && <p>与已观察的官方响应指纹相符，仅作为型号候选。</p>}
        {model.identityStatus === "reference" && <p>来自官方连接的响应记录，作为指纹参考。</p>}
        {model.identityStatus === "unknown" && <p>暂无可匹配的官方指纹，型号尚未确认。</p>}
        <dl>
          {model.identityCandidates.length > 0 && <><dt>指纹候选</dt><dd className="usage-model-candidates">{model.identityCandidates.map(name => <code key={name}>{name}</code>)}</dd></>}
          <dt>上游声明</dt><dd><code>{model.declaredModel || "未回传"}</code></dd>
          <dt>路由模型</dt><dd><code>{model.expectedModel || "未记录"}</code></dd>
          <dt>系统指纹</dt><dd><code>{model.fingerprint || "未回传"}</code></dd>
          <dt>被动指纹</dt><dd><code>{model.passiveFingerprint || "未捕获"}</code></dd>
          <dt>协议线索</dt><dd>{model.protocols.map(protocol => PROTOCOL_LABELS[protocol] || protocol).join(" · ") || "未捕获"}</dd>
          <dt>服务栈线索</dt><dd>{model.fingerprintFeatures.join(" · ") || "未捕获"}</dd>
          <dt>官方参考数</dt><dd>{model.referenceCount}</dd>
        </dl>
        <p className="usage-model-sources">开源方法：{SOURCES.map(([name, url]) => <a key={name} href={url} target="_blank" rel="noopener noreferrer">{name}</a>)}</p>
      </div>
    </details>
  </div>;
}
