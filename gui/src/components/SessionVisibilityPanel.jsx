import { useState } from "react";
import { Eye, Loader2, RefreshCw, RotateCcw } from "lucide-react";
import { Modal } from "./Dialog.jsx";
import RefinedSelect from "./RefinedSelect.jsx";

const issueLabels = {
  ambiguous_rollout:"会话文件存在多份记录", database_unusable:"数据库当前无法安全处理",
  quick_rollout_byte_budget:"已达到快速检查的读取上限", quick_rollout_count_budget:"已达到快速检查的文件上限",
  quick_rollout_invalid:"会话元数据不完整", quick_rollout_unavailable:"会话文件暂时不可读取",
  rollout_reference:"会话文件引用需要检查", rollout_warning:"会话文件需要人工检查",
  superseded_database:"已跳过旧版数据库",
  catalog_missing:"部分对话缺少目录缓存，可用深度检查继续定位",
  catalog_blocked:"部分目录记录存在冲突，已保留原记录",
};

export default function SessionVisibilityPanel({ api, notify, confirm, onRecovered }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState("quick");
  const [report, setReport] = useState(null);
  const [backups, setBackups] = useState([]);
  const [backupId, setBackupId] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const inspect = async (nextMode = mode) => {
    setOpen(true); setBusy("inspect"); setError(""); setReport(null); setMode(nextMode);
    try {
      const response = await api("/api/sessions/visibility?mode=" + nextMode, {timeoutMs:120000});
      setReport(response.inspection); setBackups(response.backups || []);
      setBackupId(response.backups?.[0]?.backupId || "");
    } catch (cause) { setError(cause.message); }
    finally { setBusy(""); }
  };
  const mutate = async (restore = false) => {
    if (!await confirm({title:restore ? "撤销这次可见性修复？" : "修复历史对话的可见性？", message:restore ? "将校验并撤销所选修复涉及的字段，保留后来新增的对话。请先完全退出 Codex。" : "将先备份，再修复有充分记录依据的历史对话索引与服务标记。请先完全退出 Codex。", confirmLabel:restore ? "校验并撤销" : "备份并修复"})) return;
    setBusy("write"); setError("");
    try {
      const response = await api("/api/sessions/visibility/" + (restore ? "restore" : "repair"), {method:"POST",body:JSON.stringify(restore ? {backupId} : {expectedToken:report.repairToken,mode}), timeoutMs:120000});
      notify(restore ? "已撤销所选修复" : response.result.message || (response.result.changed ? "可见性已修复，重新打开 Codex 后查看" : "当前记录无需修改"), response.result.completion?.partial ? "warning" : "success");
      await onRecovered?.(); await inspect();
    } catch (cause) { setError(cause.message); }
    finally { setBusy(""); }
  };
  const count = new Set((report?.plan || []).map(item => item.threadId)).size;
  return <>
    <section className="history-recovery-panel">
      <button className="history-recovery-toggle" onClick={() => inspect("quick")}><Eye size={19}/><span><strong>找回列表中不可见的对话</strong><small>检查切换账号或中转站后，对话索引与当前服务是否一致</small></span><RefreshCw size={17}/></button>
    </section>
    {open && <Modal title="历史对话可见性" description="快速检查优先读取索引；深度检查会读取更多会话文件。" wide dismissible={!busy} onClose={() => setOpen(false)}>
      <div className="modal-body form-stack">
        <div className="modal-actions">
          <button className="button secondary" disabled={Boolean(busy)} onClick={() => inspect("quick")}>快速检查</button>
          <button className="button secondary" disabled={Boolean(busy)} onClick={() => inspect("deep")}>深度检查</button>
        </div>
        {busy === "inspect" && <p role="status"><Loader2 size={16} className="spin"/> 正在检查本地会话…</p>}
        {error && <p className="inline-error" role="alert">{error}</p>}
        {report && <>
          <div className="recovery-preview-summary"><strong>{count}</strong><span>个对话可修复 · 已跳过 {report.counts?.subagentsExcluded || 0} 个子代理、{report.counts?.archivedExcluded || 0} 个归档对话</span></div>
          {report.completion?.partial && <p className="inline-note">部分记录需要继续检查，当前批次只修复已确认的正常主对话。</p>}
          <p>{!report.databases?.length ? "当前没有发现 Codex 会话数据库，无需修改。" : report.safeToRepair ? count ? "修复会保留对话正文、加密内容和原始时间。" : "当前检查范围内没有需要修复的对话。" : "部分记录存在歧义，已暂停写入。可以尝试深度检查。"}</p>
          {report.encryptedContentWarning && <p className="inline-note">账号切换不会解密另一账号的加密历史；恢复列表可见性后，继续对话仍取决于原账号权限。</p>}
          {Boolean(report.issues?.length) && <div className="history-recovery-files">{[...new Set(report.issues.map(item => item.kind))].map(kind => <div key={kind}><span>{issueLabels[kind] || "部分记录需要进一步检查"}</span></div>)}</div>}
          <button className="button primary" disabled={Boolean(busy) || !count || !report.safeToRepair} onClick={() => mutate(false)}><Eye size={16}/>备份并修复</button>
        </>}
        {backups.length > 0 && <div className="form-stack">
          <label className="field"><span>撤销已有修复</span><RefinedSelect variant="field" ariaLabel="选择可见性修复记录" value={backupId} onChange={setBackupId} options={backups.map(item => ({value:item.backupId,label:item.createdAt ? new Date(item.createdAt).toLocaleString("zh-CN") : item.backupId}))}/></label>
          <button className="button secondary" disabled={Boolean(busy) || !backupId} onClick={() => mutate(true)}><RotateCcw size={16}/>撤销所选修复</button>
        </div>}
      </div>
      <footer className="modal-footer"><button className="button secondary" disabled={Boolean(busy)} onClick={() => setOpen(false)}>关闭</button></footer>
    </Modal>}
  </>;
}
