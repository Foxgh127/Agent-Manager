import { useRef, useState } from "react";
import { Archive, ChevronLeft, ChevronRight, Loader2, RotateCcw, ShieldCheck, Trash2 } from "lucide-react";
import { Modal } from "./Dialog.jsx";
import "./ConfigRecoveryPanel.css";

const PAGE_SIZE = 5;
const KIND_LABELS = { auto: "自动", manual: "手动", recovery: "恢复前原件", external: "外部" };

export default function ConfigRecoveryPanel({ api, notify, confirm, onRestored, hasUnsavedDraft = false, disabled = false }) {
  const [open, setOpen] = useState(false);
  const [inspection, setInspection] = useState(null);
  const [backupId, setBackupId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [page, setPage] = useState(0);
  const [filter, setFilter] = useState("all");
  const inFlight = useRef(false);
  const acceptInspection = (next, preferredId = "") => {
    setInspection(next);
    setBackupId(next.backups.some(row => row.id === preferredId) ? preferredId : next.backups[0]?.id || "");
  };
  const inspect = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setOpen(true); setBusy(true); setError(""); setInspection(null); setBackupId(""); setPage(0); setFilter("all");
    try {
      const response = await api("/api/codex-config/recovery");
      acceptInspection(response.inspection);
    } catch (cause) { setError(cause.message); }
    finally { setBusy(false); inFlight.current = false; }
  };
  const mutateBackup = async (row = null) => {
    if (!inspection || inFlight.current) return;
    inFlight.current = true;
    try {
      if (row && !await confirm({ title: "删除此配置备份？", message: "只删除这份备份，当前配置不变。删除后无法从此备份恢复。", detail: row.name, confirmLabel: "删除备份", tone: "danger" })) return;
      setBusy(true); setError("");
      const response = await api(row ? "/api/codex-config/backups/delete" : "/api/codex-config/backups", {
        method: "POST", body: JSON.stringify(row ? { backupId: row.id } : { expectedFingerprint: inspection.fingerprint }),
      });
      acceptInspection(response.inspection, row ? backupId : response.result.id);
      if (!row) { setFilter("all"); setPage(0); }
      notify(response.result.warning || (row ? "备份已删除" : "手动备份已保存"), response.result.warning ? "warning" : "success");
    } catch (cause) { setError(cause.message); }
    finally { setBusy(false); inFlight.current = false; }
  };
  const repair = async (action) => {
    if (!inspection || inFlight.current) return;
    inFlight.current = true;
    const reset = action === "reset";
    const restoring = action === "restore";
    try {
      if (reset || restoring || hasUnsavedDraft) {
        const message = reset ? "将把 config.toml 重建为空配置，原文件单独备份；账号和会话文件不会删除。" : restoring ? "将用所选备份替换 config.toml，当前原文件会另存备份；不会自动关闭正在运行的 Codex。" : "将无损转换 config.toml 的编码。";
        const approved = await confirm({ title: reset ? "保留原文件并重建配置？" : restoring ? "从所选备份恢复配置？" : "转换编码并重新读取配置？", message: message + (hasUnsavedDraft ? " 配置编辑器中未保存的修改将被恢复后的内容替换。" : ""), confirmLabel: reset ? "保留并重建" : restoring ? "恢复备份" : "转换并重新读取", tone: reset ? "danger" : "warning" });
        if (!approved) return;
      }
      setBusy(true); setError("");
      const response = await api("/api/codex-config/recovery", { method: "POST", body: JSON.stringify({ expectedFingerprint: inspection.fingerprint, backupId: restoring ? backupId : null, reset }) });
      await onRestored?.();
      notify(response.result.warning || (response.result.changed ? "配置已修复，原文件已备份；重新打开 Codex 后生效" : "配置无需修改"), response.result.warning ? "warning" : "success");
      setOpen(false);
    } catch (cause) { setError(cause.message); }
    finally { setBusy(false); inFlight.current = false; }
  };
  const files = inspection?.backupFiles || inspection?.backups || [];
  const filtered = files.filter(row => filter === "all" || row.kind === filter);
  const maxPage = Math.max(0, Math.ceil(filtered.length / PAGE_SIZE) - 1);
  const visiblePage = Math.min(page, maxPage);
  const selected = files.find(row => row.id === backupId);
  return <>
    <button type="button" className="button subtle compact config-recovery-trigger" onClick={inspect} disabled={busy || disabled} aria-haspopup="dialog" title="检查并恢复 config.toml"><RotateCcw size={14} />恢复配置</button>
    {open && <Modal className="config-recovery-modal" title="检查与恢复 Codex 配置" description="只处理 config.toml；账号凭据和对话记录保持原样。" onClose={() => !inFlight.current && setOpen(false)}>
      <div className="modal-body config-recovery-content">
        {busy && !inspection && <p role="status"><Loader2 size={16} className="spin" /> 正在检查配置…</p>}
        {error && <p className="inline-error" role="alert">{error}</p>}
        {inspection && <>
          <p className="config-recovery-status"><ShieldCheck size={16} /> {inspection.message}</p>
          <div className="config-backup-heading"><div><strong>配置备份</strong><p>自动保留最近 {inspection.automaticLimit || 3} 份不同内容；手动备份长期保留。</p></div><button type="button" className="button secondary compact" disabled={busy || !inspection.exists} onClick={() => mutateBackup()}><Archive size={14} />手动备份</button></div>
          {inspection.retentionSummary && <p className="config-backup-note">自动 {inspection.retentionSummary.automatic} 份（其中受保护 {inspection.retentionSummary.protectedAutomatic} 份） · 手动 {inspection.retentionSummary.manual} 份 · 外部 {inspection.retentionSummary.external} 份</p>}
          {hasUnsavedDraft && <p className="config-backup-note">备份磁盘中的配置，编辑器未保存的修改不会包含在内。</p>}
          <div className="config-backup-filters" aria-label="备份类别">
            {[["all", "全部"], ["auto", "自动"], ["manual", "手动"], ["recovery", "恢复前原件"], ["external", "外部"]].map(([value, label]) => <button type="button" key={value} aria-pressed={filter === value} onClick={() => { setFilter(value); setPage(0); }} disabled={busy}>{label}</button>)}
          </div>
          <div className="config-backup-list" aria-label="配置备份列表">
            {filtered.slice(visiblePage * PAGE_SIZE, (visiblePage + 1) * PAGE_SIZE).map(row => <div className={`config-backup-row ${row.id === backupId ? "selected" : ""}`} key={row.id}>
              <label className="config-backup-choice"><input type="radio" name="config-backup" checked={row.id === backupId} disabled={busy || row.canRestore === false} onChange={() => setBackupId(row.id)} aria-label={`选择备份 ${row.name}`} /><span className="config-backup-copy"><span className="config-backup-summary"><time>{new Date(row.modifiedAt * 1000).toLocaleString("zh-CN", { hour12: false })}</time><span className={`config-backup-kind ${row.kind || "external"}`}>{KIND_LABELS[row.kind] || "备份"}</span><small>{(row.size / 1024).toFixed(1)} KB</small></span><span className="config-backup-name" title={row.name}>{row.name}</span>{row.canRestore === false && <small className="config-backup-note">原始内容无法解析，可保留或删除</small>}{row.protectedReason && <small className="config-backup-note">{row.protectedReason}</small>}</span></label>
              <button type="button" className="icon-button config-backup-delete" aria-label={`删除备份 ${row.name}`} title={row.protectedReason || "删除备份"} disabled={busy || !row.canDelete} onClick={() => mutateBackup(row)}><Trash2 size={16} /></button>
            </div>)}
            {!filtered.length && <p className="config-backup-empty">暂无此类备份</p>}
          </div>
          <div className="config-backup-pagination"><span>{filtered.length} 份备份{selected && <small> · 已选 {KIND_LABELS[selected.kind] || "备份"} {selected.fingerprint?.slice(0, 8)}</small>}</span><span><button type="button" className="icon-button" aria-label="上一页备份" disabled={busy || !visiblePage} onClick={() => setPage(visiblePage - 1)}><ChevronLeft size={16} /></button><small>{visiblePage + 1} / {maxPage + 1}</small><button type="button" className="icon-button" aria-label="下一页备份" disabled={busy || visiblePage === maxPage} onClick={() => setPage(visiblePage + 1)}><ChevronRight size={16} /></button></span></div>
          <p className="config-backup-note">恢复前原件计入自动保留；手动、外部和活动恢复引用除外。仍需用于恢复的文件会显示保护原因。</p>
          {inspection.status === "invalid" && !inspection.backups.length && <p>未找到可解析的配置备份。可回到原始编辑器修正，或保留原文件后重建空配置。</p>}
        </>}
      </div><footer className="modal-footer">
        <button type="button" className="button secondary" disabled={busy} onClick={() => setOpen(false)}>关闭</button>
        {inspection?.status === "invalid" && <button type="button" className="button danger-outline" disabled={busy} onClick={() => repair("reset")}>保留原文件并重建</button>}
        {inspection?.canNormalize && <button type="button" className="button secondary" disabled={busy} onClick={() => repair("normalize")}>无损转换为 UTF-8</button>}
        {backupId && <button type="button" className="button primary" disabled={busy} onClick={() => repair("restore")}>{busy ? "处理中…" : "恢复所选备份"}</button>}
      </footer>
    </Modal>}
  </>;
}
