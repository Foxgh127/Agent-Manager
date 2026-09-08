import { useEffect, useState } from "react";
import { AlertTriangle, Loader2, RefreshCw, Save } from "lucide-react";
import { Modal } from "./Dialog.jsx";

export default function SessionSyncModal({ history, onClose, onDone, notify, api, formatBytes }) {
  const current = history.sync || {};
  const [target, setTarget] = useState(current.target || "");
  const [direction, setDirection] = useState(current.direction || "two_way");
  const [recentDays, setRecentDays] = useState(
    Number(current.recentDays ?? 30),
  );
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => { setPreview(null); setError(""); }, [target, direction, recentDays]);
  const input = () => {
    if (String(recentDays).trim() === "" || !Number.isInteger(Number(recentDays))) throw new Error("请填写同步天数；填写 0 表示全部会话");
    return { target: target.trim(), direction, recentDays: Number(recentDays) };
  };
  const previewSync = async () => {
    setBusy(true); setError("");
    try {
      const result = await api("/api/history/preview", { method: "POST", body: JSON.stringify(input()) });
      if (!result.preview?.fingerprint) throw new Error("未收到有效同步计划，请重新预览");
      setPreview(result.preview);
    } catch (cause) { setError(cause.message); }
    finally { setBusy(false); }
  };
  const submit = async (runNow) => {
    setBusy(true);
    setError("");
    try {
      const payload = input();
      if (runNow) {
        if (!preview?.fingerprint) throw new Error("请先预览同步计划");
        const result = await api("/api/history/sync", {
          method: "POST",
          body: JSON.stringify({ ...payload, expectedFingerprint: preview.fingerprint }),
        });
        const summary = result.result;
        notify(
          `会话同步完成：拉取 ${summary.pulled}，推送 ${summary.pushed}${summary.conflicts ? `，${summary.conflicts} 个分叉保留两端` : ""}${summary.warning ? `；${summary.warning}` : ""}`,
          summary.conflicts || summary.warning ? "warning" : "success",
        );
      } else {
        await api("/api/history/settings", { method: "POST", body: JSON.stringify(payload) });
        notify("会话同步设置已保存");
      }
      await onDone();
      onClose();
    } catch (error) {
      setPreview(null);
      setError(error.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title="会话备份与同步"
      description="先预览再执行；仅推送可备份正在使用的会话，分叉内容会保留两端。"
      onClose={onClose}
      dismissible={!busy}
    >
      <div className="modal-body form-stack">
        {error && <div className="sync-preview-error" role="alert"><AlertTriangle size={18} /><span>{error}</span></div>}
        <label className="field">
          <span>同步文件夹</span>
          <input
            disabled={busy}
            value={target}
            onChange={(event) => setTarget(event.target.value)}
            placeholder="例如 D:\\Codex History"
          />
        </label>
        {!!history.recommendedTargets?.length && (
          <div className="recommended-targets">
            <span>推荐位置</span>
            <div>
              {history.recommendedTargets.map((item) => (
                <button
                  type="button"
                  key={item.path}
                  disabled={busy || !item.available}
                  onClick={() => setTarget(item.path)}
                >
                  <strong>{item.label}</strong>
                  <small>{item.path}</small>
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="form-grid two">
          <label className="field">
            <span>同步方向</span>
            <select
              disabled={busy}
              value={direction}
              onChange={(event) => setDirection(event.target.value)}
            >
              <option value="two_way">双向同步</option>
              <option value="push">仅推送到同步盘</option>
              <option value="pull">仅拉取到本机</option>
            </select>
          </label>
          <label className="field">
            <span>最近天数（0 为全部）</span>
            <input
              disabled={busy}
              type="number"
              min="0"
              max="3650"
              value={recentDays}
              onChange={(event) => setRecentDays(event.target.value)}
            />
          </label>
        </div>
        {preview && <section className="sync-preview" aria-label="同步计划预览">
          <header><strong>同步计划</strong><span>{formatBytes(preview.copyBytes)} 待复制</span></header>
          <p>本次按预览时的文件位置备份；之后新增的会话内容可在下一次同步。</p>
          <div className="sync-preview-counts"><span>推送 <b>{(preview.counts?.copy_to_remote || 0) + (preview.counts?.replace_remote || 0)}</b></span><span>拉取 <b>{(preview.counts?.copy_to_local || 0) + (preview.counts?.replace_local || 0)}</b></span><span>分叉保留 <b>{preview.counts?.conflict || 0}</b></span></div>
          {preview.requiresCodexClosed && <p>此计划会写入本机会话，请在执行前关闭 Codex。</p>}
          <ul>{(preview.operations || []).slice(0, 12).map(item => <li key={item.relative}><span title={item.relative}>{item.relative.split("/").at(-1)}</span><small>{item.reason}</small></li>)}</ul>
          {!preview.operations?.length && <p>两端内容一致，没有需要复制的会话。</p>}
          {(preview.truncated || preview.operations?.length > 12) && <small>仅展示前 12 项；执行会处理预览中的全部操作。</small>}
        </section>}
        <div className="modal-actions split">
          <button
            className="button secondary"
            disabled={busy}
            onClick={() => submit(false)}
          >
            <Save size={16} />
            仅保存
          </button>
          <button
            className="button primary"
            disabled={busy || !target.trim()}
            onClick={preview ? () => submit(true) : previewSync}
          >
            {busy ? (
              <Loader2 className="spin" size={16} />
            ) : (
              <RefreshCw size={16} />
            )}
            {preview ? "确认同步" : "预览同步计划"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

