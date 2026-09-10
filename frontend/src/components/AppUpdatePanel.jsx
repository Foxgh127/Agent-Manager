import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Download, Loader2, RefreshCw } from "lucide-react";
import "./AppUpdatePanel.css";
import { loadAppUpdate, rememberAppUpdate, subscribeAppUpdate } from "../appUpdateResource.js";

const labels = { unconfigured: "此构建尚未绑定发布仓库", config_error: "发布源读取失败", not_checked: "等待检查更新", checking: "正在检查", check_failed: "检查失败，可重试", current: "已是最新版" };

export default function AppUpdatePanel({ api, notify, refreshKey = 0 }) {
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const autoInstall = useRef(false);
  const mounted = useRef(true);
  const inFlight = useRef(false);
  const installStarted = useRef(false);
  const lastRefreshKey = useRef(refreshKey);
  const notifyRef = useRef(notify);
  notifyRef.current = notify;

  const install = useCallback(async () => {
    if (installStarted.current) return;
    installStarted.current = true;
    setBusy("install");
    try {
      const r = await api("/api/app-update/install", { method: "POST", body: "{}", timeoutMs: 60000 });
      notifyRef.current?.(r.message || "正在完成更新并重启管理器", "success");
    } catch (cause) {
      installStarted.current = false;
      if (mounted.current) { setError(cause.message); setBusy(""); }
    }
  }, [api]);

  const accept = useCallback(next => {
    if (!mounted.current || !next) return;
    rememberAppUpdate(api, next);
    setStatus(next);
    if (next.installation?.state === "failed") {
      installStarted.current = false; setBusy(""); setError(next.installation.message || "更新未完成，请重试");
    }
    if (["waiting_for_exit", "installed"].includes(next.installation?.state)) {
      installStarted.current = true; setBusy("install");
    }
    if (next.download?.autoInstallQueued) autoInstall.current = false;
    if (next.download?.state === "ready" && autoInstall.current) {
      autoInstall.current = false;
      if (next.canInstall) install();
      else notifyRef.current?.("更新已下载并校验，可在下载目录使用新版本。", "success");
    }
    if (["failed", "cancelled"].includes(next.download?.state)) autoInstall.current = false;
  }, [api, install]);

  const check = useCallback(async (force = true) => {
    if (inFlight.current || installStarted.current) return;
    inFlight.current = true;
    if (force) setBusy("check"); setError("");
    try {
      accept(await loadAppUpdate(api, { force }));
    } catch (cause) { if (mounted.current) setError(cause.message); }
    finally { inFlight.current = false; if (mounted.current && !installStarted.current) setBusy(""); }
  }, [api, accept]);

  useEffect(() => {
    mounted.current = true;
    const unsubscribe = subscribeAppUpdate(api, accept);
    check(false);
    return () => { mounted.current = false; unsubscribe(); };
  }, [api, accept, check]);
  useEffect(() => {
    if (lastRefreshKey.current === refreshKey) return;
    lastRefreshKey.current = refreshKey;
    check(true);
  }, [check, refreshKey]);
  const downloading = status?.download?.state === "downloading";
  useEffect(() => {
    if (!downloading && busy !== "install" && !(status?.download?.autoInstallQueued && status?.download?.state === "ready" && status?.installation?.state !== "failed")) return undefined;
    let polling = false;
    const timer = window.setInterval(async () => {
      if (polling) return;
      polling = true;
      try { const r = await api("/api/app-update"); accept(r.status); }
      catch (cause) { if (mounted.current) setError(cause.message); }
      finally { polling = false; }
    }, 1200);
    return () => window.clearInterval(timer);
  }, [downloading, busy, status?.download?.autoInstallQueued, status?.download?.state, status?.installation?.state, api, accept]);

  const update = async () => {
    if (inFlight.current || installStarted.current) return;
    setError("");
    if (status?.canInstall) { await install(); return; }
    inFlight.current = true;
    setBusy("download"); autoInstall.current = Boolean(status?.installSupported);
    try {
      const r = await api("/api/app-update/download", { method: "POST", body: JSON.stringify({ releaseToken: status?.latestRelease?.releaseToken, installAfterDownload: Boolean(status?.installSupported) }) });
      accept(r.status);
    } catch (cause) { autoInstall.current = false; setError(cause.message); }
    finally { inFlight.current = false; if (!installStarted.current) setBusy(""); }
  };
  const progress = Math.min(100, Math.round((status?.download?.downloadedBytes || 0) / (status?.download?.totalBytes || 1) * 100));
  const failure = error || status?.error || status?.download?.error || (status?.installation?.state === "failed" ? status.installation.message : "");
  const message = busy === "install" ? "正在恢复配置并重启…" : downloading ? `下载并校验 ${progress}%` : failure || (status?.updateAvailable ? `可更新到 ${status.latestRelease?.version}` : labels[status?.state] || "正在读取版本");
  return <article className="update-component manager-update" aria-label="Agent Manager 更新">
    <span className="update-icon manager"><RefreshCw size={20} /></span>
    <div className="update-copy"><small>AGENT MANAGER</small><strong>{status?.currentVersion || "—"}</strong><p className={failure ? "update-error" : ""} title={message} role="status">{message}</p></div>
    {downloading || busy ? <span className="status-pill"><Loader2 size={14} className="spin" />{downloading ? `${progress}%` : busy === "install" ? "重启中" : "处理中"}</span>
      : status?.canDownload || status?.canInstall ? <button className="button primary compact" onClick={update} title="下载校验后关闭 Codex、恢复配置，并重启管理器"><Download size={14} />{status.installSupported ? "一键更新" : "下载更新"}</button>
      : <button className="button secondary compact" onClick={() => check(true)} disabled={!status?.configured}>{status?.state === "current" ? <Check size={14} /> : <RefreshCw size={14} />}{status?.state === "current" ? "已是最新版" : "检查更新"}</button>}
    {downloading && <progress className="manager-update-progress" max="100" value={progress} aria-label={`下载进度 ${progress}%`} />}
    {downloading && <button className="button subtle compact manager-update-cancel" onClick={async () => { autoInstall.current = false; try { const r = await api("/api/app-update/cancel", { method: "POST", body: "{}" }); accept(r.status); } catch (e) { setError(e.message); } }}>取消</button>}
  </article>;
}
