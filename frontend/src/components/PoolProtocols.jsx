import { ChevronDown, Route } from "lucide-react";
import "./PoolProtocols.css";

const labels = {responses:"Responses",chatCompletions:"Chat Completions",compact:"上下文压缩",inputTokens:"输入 Token 计数"};
const modes = {native_http:"支持",responses_conversion:"协议转换",native_compaction_trigger_bridge:"压缩桥接",passthrough:"随上游",passthrough_no_generation_stats:"随上游",unsupported:"暂不支持"};

export default function PoolProtocols({ capabilities }) {
  if (!capabilities?.endpoints) return null;
  return <details className="pool-protocols">
    <summary><Route size={18}/><span><strong>协议与接入能力</strong><small>Responses · Chat · 压缩{capabilities.transports?.websocket ? " · WebSocket" : ""}</small></span><ChevronDown size={17}/></summary>
    <div className="pool-protocol-body">
      <div className="pool-protocol-table" role="table" aria-label="API 协议支持范围">
        <div role="row" className="pool-protocol-heading"><span role="columnheader">接口</span><span role="columnheader">官方账号</span><span role="columnheader">API 上游</span></div>
        {Object.entries(capabilities.endpoints).map(([id, endpoint]) => <div role="row" key={id}>
          <span role="cell"><strong>{labels[id] || id}</strong><code>{endpoint.path}</code></span>
          <span role="cell" className={endpoint.account === "unsupported" ? "unavailable" : ""}>{modes[endpoint.account] || "按服务能力"}</span>
          <span role="cell">{modes[endpoint.provider] || "按服务能力"}</span>
        </div>)}
      </div>
      {capabilities.transports?.websocketMode === "serial_http_sse_bridge" && <p>WebSocket 每条连接串行处理 Responses 请求，使用 HTTP/SSE 后端；不提供多路复用和实时语音。</p>}
    </div>
  </details>;
}
