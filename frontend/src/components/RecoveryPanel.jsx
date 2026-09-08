import { useCallback, useEffect, useState } from "react";
import { ArchiveRestore, Check, DatabaseBackup, FolderLock, Loader2, Plus, RefreshCw, RotateCcw, ShieldCheck, Trash2 } from "lucide-react";
import { Modal } from "./Dialog.jsx";

const formatSize = value => Number(value || 0) < 1024 ? `${Number(value || 0)} B` : Number(value || 0) < 1024 * 1024 ? `${(Number(value || 0) / 1024).toFixed(1)} KB` : `${(Number(value || 0) / 1024 / 1024).toFixed(1)} MB`;
const actionLabels = { unchanged: "无变化", restore: "恢复", create: "重建", remove: "移除" };

export default function RecoveryPanel({ api, notify, confirm, onRestored, gatewayRunning = false }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [working, setWorking] = useState("");
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [scope, setScope] = useState("configuration");
  const [preview, setPreview] = useState(null);
  const [expanded, setExpanded] = useState(false);
  const [needsApply, setNeedsApply] = useState(false);
  const load = useCallback(async () => {
    setLoading(true); setError("");
    try { setData(await api("/api/recovery")); }
    catch (cause) { setError(cause.message); }
    finally { setLoading(false); }
  }, [api]);
  useEffect(() => { load(); }, [load]);
  const create = async () => {
    setWorking("create"); setError("");
    try {
      const response = await api("/api/recovery", { method: "POST", body: JSON.stringify({ scope, name }) });
      if (!response.point?.id) throw new Error("未收到有效恢复点，请刷新列表核对结果");
      setName(""); await load(); notify("加密恢复点已创建");
    } catch (cause) { setError(cause.message); }
    finally { setWorking(""); }
  };
  const inspect = async point => {
    setWorking(point.id); setError("");
    try {
      const response = await api(`/api/recovery/${encodeURIComponent(point.id)}/preview`, { method: "POST", body: "{}" });
      setPreview(response.result);
    } catch (cause) { setError(cause.message); }
    finally { setWorking(""); }
  };
  const restore = async () => {
    if (!preview) return;
    const approved = await confirm({ tone: "warning", title: `恢复“${preview.point.name}”？`,
      message: `将按预览更新 ${preview.changed} 个文件；当前内容会先保存为新的安全备份。`,
      detail: "配置恢复需要关闭 Codex 并停止本地反代。恢复后可重新应用配置；不会自动切换正在使用的登录。", confirmLabel: "备份当前内容并恢复" });
    if (!approved) return;
    setWorking("restore"); setError("");
    try {
      const result = await api(`/api/recovery/${encodeURIComponent(preview.point.id)}/restore`, { method: "POST", body: JSON.stringify({ expectedFingerprint: preview.fingerprint }) });
      setNeedsApply(result.result.scope === "configuration");
      setPreview(null); await load(); await onRestored(); notify(`已恢复 ${result.result.changed} 个文件；恢复前的状态也已保留`);
    } catch (cause) { setError(cause.message); }
    finally { setWorking(""); }
  };
  const remove = async point => {
    if (!(await confirm({ tone: "danger", title: `删除恢复点“${point.name}”？`, message: "仅删除这份备份，不影响当前账号、配置或会话。", confirmLabel: "删除恢复点" }))) return;
    setWorking(point.id);
    try { await api(`/api/recovery/${encodeURIComponent(point.id)}/delete`, { method: "POST", body: "{}" }); await load(); }
    catch (cause) { setError(cause.message); }
    finally { setWorking(""); }
  };
  const apply = async () => {
    setWorking("apply"); setError("");
    try { await api("/api/apply", { method: "POST", body: JSON.stringify({ syncSecrets: false }) }); await onRestored(); setNeedsApply(false); notify("恢复后的调度配置已应用"); }
    catch (cause) { setError(cause.message); }
    finally { setWorking(""); }
  };
  const points = data?.points || [];
  return <section className="settings-card recovery-panel full">
    <header><span><DatabaseBackup size={20} /></span><div><h2>备份与恢复</h2><p>修改之前留一个恢复点，出问题时有据可退。</p></div><button className="icon-button" aria-label="刷新恢复点列表" onClick={load} disabled={loading}><RefreshCw size={16} className={loading ? "spin" : ""} /></button></header>
    <div className="recovery-body">
      <div className="recovery-scope-note"><FolderLock size={18} /><span>本机加密备份包含账号配置和保险库，仅创建备份的 Windows 用户可恢复。会话内容请使用调度中心的“会话备份与同步”。</span></div>
      <div className="recovery-create"><label><span className="visually-hidden">恢复点名称</span><input value={name} onChange={event => setName(event.target.value)} placeholder="恢复点名称（可选）" maxLength={120} /></label><label><span className="visually-hidden">备份范围</span><select value={scope} onChange={event => setScope(event.target.value)}><option value="configuration">配置与账号</option><option value="usage">用量统计</option></select></label><button className="button primary" onClick={create} disabled={Boolean(working)}>{working === "create" ? <Loader2 size={16} className="spin" /> : <Plus size={16} />}创建恢复点</button></div>
      {error && <div className="sync-preview-error" role="alert">{error}</div>}
      {needsApply && <div className="recovery-apply"><span><Check size={16} />备份已恢复，可以应用到 Codex 调度配置。</span><button className="button secondary" disabled={Boolean(working)} onClick={apply}>应用恢复后的配置</button></div>}
      {!!data?.invalidCount && <p className="recovery-warning">{data.invalidCount} 份备份清单无法读取，原文件已保留。</p>}
      <div className="recovery-list">{points.slice(0, expanded ? 200 : 6).map(point => <article key={point.id}>
        <span className="recovery-item-icon"><ArchiveRestore size={18} /></span><div><strong>{point.name}</strong><small>{new Date(point.createdAt).toLocaleString("zh-CN")} · {point.scope === "configuration" ? "配置与账号" : "用量"} · {formatSize(point.totalBytes)}{point.reason === "before_restore" ? " · 恢复前自动保存" : ""}</small></div>
        <button className="button secondary" disabled={Boolean(working)} onClick={() => inspect(point)}>{working === point.id ? <Loader2 size={15} className="spin" /> : <RotateCcw size={15} />}预览恢复</button><button className="icon-button" aria-label={`删除恢复点 ${point.name}`} disabled={Boolean(working)} onClick={() => remove(point)}><Trash2 size={15} /></button>
      </article>)}</div>
      {!points.length && !loading && <div className="recovery-empty"><ShieldCheck size={24} /><strong>还没有恢复点</strong><span>在批量编辑、清理或调整配置之前，创建第一份备份。</span></div>}
      {points.length > 6 && <button className="button subtle" onClick={() => setExpanded(value => !value)}>{expanded ? "收起" : `查看全部 ${points.length} 份备份`}</button>}
    </div>
    {preview && <Modal title={`恢复预览 · ${preview.point.name}`} description="确认文件变化后再恢复；恢复前会自动保存当前内容。" onClose={() => setPreview(null)} dismissible={working !== "restore"}>
      <div className="modal-body form-stack"><div className="recovery-preview-summary"><strong>{preview.changed}</strong><span>个文件将改变</span></div>
        {gatewayRunning && <p className="recovery-warning">本地反代正在运行，请先停止服务再恢复。</p>}
        {preview.requiresCodexClosed && <p>恢复配置前需要关闭 Codex。仅预览和创建备份无需关闭。</p>}
        {error && <div role="alert" className="sync-preview-error">{error}</div>}
        <div className="recovery-preview-files">{preview.files.map(file => <div key={file.key}><span>{file.label}</span><small>{formatSize(file.currentBytes)} → {formatSize(file.backupBytes)}</small><b className={file.action}>{actionLabels[file.action]}</b></div>)}</div>
        <div className="modal-actions"><button className="button secondary" onClick={() => setPreview(null)} disabled={working === "restore"}>取消</button><button className="button primary" onClick={restore} disabled={!preview.changed || Boolean(working) || gatewayRunning}>{working === "restore" ? <Loader2 size={16} className="spin" /> : <RotateCcw size={16} />}恢复这份备份</button></div>
      </div>
    </Modal>}
  </section>;
}
