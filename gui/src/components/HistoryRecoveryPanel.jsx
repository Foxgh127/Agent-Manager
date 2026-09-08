import { useState } from "react";
import { AlertTriangle, ChevronDown, FolderClock, Loader2, RefreshCw, RotateCcw } from "lucide-react";
import { Modal } from "./Dialog.jsx";

const actions = { restore: "恢复原内容", remove: "移除本次新增", unchanged: "无需改变", conflict: "保留外部修改" };

export default function HistoryRecoveryPanel({ api, confirm, notify, onRecovered }) {
  const [expanded, setExpanded] = useState(false);
  const [data, setData] = useState(null);
  const [working, setWorking] = useState("");
  const [error, setError] = useState("");
  const [preview, setPreview] = useState(null);
  const load = async () => {
    setWorking("load"); setError("");
    try { setData(await api("/api/history/recoveries")); }
    catch (cause) { setError(cause.message); }
    finally { setWorking(""); }
  };
  const inspect = async item => {
    setWorking(item.id); setError("");
    try {
      const response = await api(`/api/history/recoveries/${encodeURIComponent(item.id)}/preview`, { method: "POST", body: "{}" });
      if (!response.result?.fingerprint) throw new Error("恢复计划不完整，请重新检查");
      setPreview(response.result);
    } catch (cause) { setError(cause.message); }
    finally { setWorking(""); }
  };
  const restore = async () => {
    if (!(await confirm({ title: "恢复中断前的会话文件？", message: preview.changed ? `将撤回这次同步造成的 ${preview.changed} 项变化，执行前会保存当前文件。` : "文件已经与同步前一致，将保留证据并结束这条恢复记录。", detail: "请先关闭 Codex；预览后发生变化或有外部修改的文件会阻止恢复。", confirmLabel: "保留备份并恢复" }))) return;
    setWorking("restore"); setError("");
    try {
      const response = await api(`/api/history/recoveries/${encodeURIComponent(preview.recovery.id)}/restore`, { method: "POST", body: JSON.stringify({ expectedFingerprint: preview.fingerprint }) });
      if (!response.result?.restored) throw new Error(response.result?.message || "恢复未完成，请重新检查计划");
      setPreview(null); await load(); await onRecovered(); notify("中断的会话同步已恢复，原始证据和安全备份已保留");
    } catch (cause) { setError(cause.message); }
    finally { setWorking(""); }
  };
  return <section className="history-recovery-panel">
    <button className="history-recovery-toggle" aria-expanded={expanded} onClick={() => { setExpanded(!expanded); if (!expanded && !data) load(); }}>
      <FolderClock size={19} /><span><strong>中断同步恢复</strong><small>检查异常退出后留下的同步记录，预览并恢复原文件</small></span><ChevronDown size={17} />
    </button>
    {expanded && <div className="history-recovery-body">
      <div className="history-recovery-intro"><p>仅经过本机校验的记录可以自动恢复；旧记录或损坏记录会保留给人工检查。</p><button className="button subtle" disabled={Boolean(working)} onClick={load}><RefreshCw size={15} />重新检查</button></div>
      {error && <div className="sync-preview-error" role="alert"><AlertTriangle size={17} />{error}</div>}
      {working === "load" && <p role="status"><Loader2 className="spin" size={16} /> 正在检查同步记录…</p>}
      {!working && data && !data.recoveries?.length && <p>没有待恢复的同步记录。</p>}
      {(data?.recoveries || []).map(item => <article key={item.id}>
        <div><strong>{item.createdAt ? new Date(item.createdAt).toLocaleString("zh-CN") : "需要人工检查的同步记录"}</strong><small>{item.reason || `${item.operationCount || 0} 项文件操作`}</small><code>{item.id}</code></div>
        <button className="button secondary" disabled={Boolean(working) || !item.recoverable} onClick={() => inspect(item)}>{working === item.id ? <Loader2 size={15} className="spin" /> : <RotateCcw size={15} />}{item.recoverable ? "预览恢复" : "保留待检查"}</button>
      </article>)}
      {data?.directory && <p className="history-evidence-path">原始记录位置：<code>{data.directory}</code></p>}
      {data?.truncated && <p>记录较多，本次仅显示部分项目；原始文件均已保留。</p>}
    </div>}
    {preview && <Modal title="中断同步恢复预览" description="将文件恢复到这次同步之前，当前内容会另存为安全备份。" dismissible={working !== "restore"} onClose={() => setPreview(null)}>
      <div className="modal-body form-stack">
        <div className="recovery-preview-summary"><strong>{preview.changed}</strong><span>项待恢复 · {preview.conflicts} 项冲突</span></div>
        <p>执行前需关闭 Codex。{preview.conflicts ? "检测到外部修改，自动恢复已暂停。" : "请核对下列会话文件。"}</p>
        {error && <div className="sync-preview-error" role="alert">{error}</div>}
        <div className="history-recovery-files">{preview.files.slice(0, 200).map(file => <div key={`${file.side}:${file.relative}`}><span>{file.side === "local" ? "本机" : "同步目录"} · {file.relative}</span><b>{actions[file.action] || file.action}</b>{file.conflict && <small>{typeof file.conflict === "string" ? file.conflict : "文件已被其他操作修改"}</small>}</div>)}</div>
        {(preview.truncated || preview.files.length > 200) && <p>此处显示前 {Math.min(200, preview.files.length)} 项，记录共 {preview.recovery.operationCount} 项；执行会校验和处理整个计划。</p>}
        <div className="modal-actions"><button className="button secondary" disabled={working === "restore"} onClick={() => setPreview(null)}>取消</button><button className="button primary" disabled={Boolean(working) || Boolean(preview.conflicts)} onClick={restore}><RotateCcw size={16} />{preview.changed ? "恢复到同步前" : "确认文件已一致"}</button></div>
      </div>
    </Modal>}
  </section>;
}
