import { useRef, useState } from "react";
import { Check, ChevronDown, Loader2, Wrench } from "lucide-react";
import HistoryRecoveryPanel from "./HistoryRecoveryPanel.jsx";
import SessionVisibilityPanel from "./SessionVisibilityPanel.jsx";

export default function SessionRepairPanel({ api, notify, confirm, onRecovered, busy = false, onBusyChange }) {
  const [working, setWorking] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const repair = async () => {
    if (inFlight.current || busy) return;
    inFlight.current = true;
    setWorking(true); onBusyChange?.(true); setError(""); setResult(null);
    try {
      const response = await api("/api/sessions/repair", { method: "POST", body: "{}", timeoutMs: 180000 });
      if (!response.result?.status) throw new Error("未收到修复结果，请重新检查。");
      setResult(response.result);
      notify(response.result.message, response.result.partial ? "warning" : "success");
      await onRecovered?.();
    } catch (cause) { setError(cause.message); }
    finally { inFlight.current = false; setWorking(false); onBusyChange?.(false); }
  };
  return <section className="session-repair-panel" aria-busy={working}>
    <div className="session-repair-heading">
      <span className="session-repair-icon"><Wrench size={20} /></span>
      <div><strong>会话修复</strong><p>修复切号后的会话打不开、列表缺失和中断同步。自动备份，需要时会重启 Codex。</p></div>
      <button className="button primary" type="button" disabled={busy || working} onClick={repair}>
        {working ? <Loader2 size={16} className="spin" /> : <Wrench size={16} />}{working ? "正在修复…" : "一键修复"}
      </button>
    </div>
    {working && <p className="session-repair-status" role="status">正在检查、备份并修复会话，请等待操作完成…</p>}
    {error && <p className="session-repair-status inline-error" role="alert">{error}</p>}
    {result && <div className={`session-repair-result ${result.partial ? "partial" : ""}`} role="status">
      {!result.partial && <Check size={17} />}<span>{result.message}</span>
      {result.visibility?.message && <p>{result.visibility.message}</p>}
      {Boolean(result.warnings?.length) && <ul>{[...new Set(result.warnings)].map(warning => <li key={warning}>{warning}</li>)}</ul>}
    </div>}
    <details className="session-repair-details">
      <summary><ChevronDown size={15} />修复详情与撤销</summary>
      <fieldset disabled={busy || working}>
        <HistoryRecoveryPanel api={api} confirm={confirm} notify={notify} onRecovered={onRecovered} />
        <SessionVisibilityPanel api={api} confirm={confirm} notify={notify} onRecovered={onRecovered} />
      </fieldset>
    </details>
  </section>;
}
