import { useState } from "react";
import { Loader2, RotateCcw, ShieldCheck } from "lucide-react";
import { Modal } from "./Dialog.jsx";
import RefinedSelect from "./RefinedSelect.jsx";

export default function ConfigRecoveryPanel({ api, notify, confirm, onRestored, hasUnsavedDraft = false, disabled = false }) {
  const [open, setOpen] = useState(false);
  const [inspection, setInspection] = useState(null);
  const [backupId, setBackupId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inspect = async () => {
    setOpen(true); setBusy(true); setError(""); setInspection(null); setBackupId("");
    try {
      const response = await api("/api/codex-config/recovery");
      setInspection(response.inspection);
      setBackupId(response.inspection.backups[0]?.id || "");
    } catch (cause) { setError(cause.message); }
    finally { setBusy(false); }
  };
  const repair = async (reset = false) => {
    if (!inspection) return;
    const restoring = backupId && !inspection.canNormalize && !reset;
    if (reset || restoring || hasUnsavedDraft) {
      const message = reset ? "将把 config.toml 重建为空配置，原文件单独备份；账号和会话文件不会删除。" : restoring ? "将用所选备份替换 config.toml，当前原文件会另存备份；不会自动关闭正在运行的 Codex。" : "将无损转换 config.toml 的编码。";
      const approved = await confirm({ title:reset ? "保留原文件并重建配置？" : restoring ? "从所选备份恢复配置？" : "转换编码并重新读取配置？", message:message + (hasUnsavedDraft ? " 配置编辑器中未保存的修改将被恢复后的内容替换。" : ""), confirmLabel:reset ? "保留并重建" : restoring ? "恢复备份" : "转换并重新读取", tone:reset ? "danger" : "warning" });
      if (!approved) return;
    }
    setBusy(true); setError("");
    try {
      const response = await api("/api/codex-config/recovery", { method:"POST", body:JSON.stringify({expectedFingerprint:inspection.fingerprint,backupId:restoring ? backupId : null,reset}) });
      await onRestored?.();
      notify(response.result.changed ? "配置已修复，原文件已备份；重新打开 Codex 后生效" : "配置无需修改");
      setOpen(false);
    } catch (cause) { setError(cause.message); }
    finally { setBusy(false); }
  };
  return <>
    <button type="button" className="button subtle compact config-recovery-trigger" onClick={inspect} disabled={busy || disabled} aria-haspopup="dialog" title="检查并恢复 config.toml"><RotateCcw size={14} />恢复配置</button>
    {open && <Modal title="检查与恢复 Codex 配置" description="只处理 config.toml；账号凭据和对话记录保持原样。" onClose={() => !busy && setOpen(false)}>
      <div className="modal-body model-reasoning-content">
        {busy && !inspection && <p role="status"><Loader2 size={16} className="spin" /> 正在检查配置…</p>}
        {error && <p className="inline-error">{error}</p>}
        {inspection && <><p><ShieldCheck size={16} /> {inspection.message}</p>
          {!inspection.canNormalize && inspection.backups.length > 0 && <label className="field"><span>有效备份</span><RefinedSelect ariaLabel="选择 Codex 配置备份" variant="field" value={backupId} onChange={setBackupId} disabled={busy} options={inspection.backups.map(backup => ({value:backup.id,label:new Date(backup.modifiedAt * 1000).toLocaleString("zh-CN"),detail:backup.name}))} /></label>}
          {inspection.status === "invalid" && !inspection.backups.length && <p>未找到可解析的配置备份。你可以回到原始编辑器手动修正，或明确选择保留原文件后重建空配置。</p>}
        </>}
      </div><footer className="modal-footer">
        <button className="button secondary" disabled={busy} onClick={() => setOpen(false)}>关闭</button>
        {inspection?.status === "invalid" && <button className="button danger-outline" disabled={busy} onClick={() => repair(true)}>保留原文件并重建</button>}
        {(inspection?.canNormalize || backupId) && <button className="button primary" disabled={busy} onClick={() => repair(false)}>{busy ? "处理中…" : inspection.canNormalize ? "无损转换为 UTF-8" : "恢复所选备份"}</button>}
      </footer>
    </Modal>}
  </>;
}
