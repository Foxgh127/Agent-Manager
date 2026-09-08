import { relayReconnectTarget } from "../relayReconnect.js";

export default function RelayOriginNotice({ details, onReconnect, disabled = false }) {
  const target = relayReconnectTarget(details);
  if (!target) return null;
  return <div className="inline-notice warning relay-origin-notice" role="status">
    <div><strong>网站跳转到了不同地址</strong><p>原地址：{details.savedOrigin}<br />当前地址：{target}</p><p>使用当前地址重新登录并确认导入。原卡片会保留，旧登录凭据不会转送到新地址。</p></div>
    <button type="button" className="button secondary compact" disabled={disabled} onClick={() => onReconnect(target)}>使用当前地址重新连接</button>
  </div>;
}
