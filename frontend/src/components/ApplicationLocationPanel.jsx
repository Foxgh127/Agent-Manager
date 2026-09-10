import { useCallback, useEffect, useRef, useState } from "react";
import { FolderOpen, Link2, Loader2, RefreshCw } from "lucide-react";
import { Modal } from "./Dialog.jsx";
import "./ApplicationLocationPanel.css";

const activeStates = new Set(["prepared", "waiting_for_exit", "copying", "verifying_startup"]);

export default function ApplicationLocationPanel({ api, notify, disabled = false, embedded = false }) {
  const [location, setLocation] = useState(null);
  const [open, setOpen] = useState(false);
  const [directory, setDirectory] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [moving, setMoving] = useState(false);
  const [shortcutInfo, setShortcutInfo] = useState(null);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const load = useCallback(async () => {
    const result = await api("/api/application/location");
    if (mounted.current) {
      setLocation(result.location);
      const move = result.location?.relocation;
      if (move?.helperRunning && !["complete", "failed"].includes(move.state)) setMoving(true);
      if (move && !move.helperRunning && (!activeStates.has(move.state) || move.helperRunning === false)) {
        setMoving(false);
        if (move.state === "failed") setError(move.message || "移动未完成，请重试");
      }
    }
    return result.location;
  }, [api]);
  useEffect(() => {
    mounted.current = true;
    load().catch(cause => { if (mounted.current) setError(cause.message); });
    return () => { mounted.current = false; };
  }, [load]);
  useEffect(() => {
    if (!moving) return undefined;
    let polling = false;
    const deadline = Date.now() + 240000;
    const timer = window.setInterval(async () => {
      if (Date.now() >= deadline) {
        window.clearInterval(timer);
        setMoving(false); setError("尚未收到移动结果，请刷新查看应用位置");
        return;
      }
      if (polling) return;
      polling = true;
      try { await load(); } catch { /* Old service exits during the handoff. */ }
      finally { polling = false; }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [moving, load]);

  const operate = async (kind, action) => {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(kind); setError("");
    try { await action(); }
    catch (cause) { if (mounted.current) setError(cause.message); }
    finally { inFlight.current = false; if (mounted.current) setBusy(""); }
  };
  const browse = () => operate("browse", async () => {
    const result = await api("/api/application/location/select", { method: "POST", body: "{}", timeoutMs: 300000 });
    if (result.directory) setDirectory(result.directory);
  });
  const move = () => operate("move", async () => {
    const result = await api("/api/application/location/move", {
      method: "POST", body: JSON.stringify({ directory: directory.trim() }), timeoutMs: 60000,
    });
    if (result.result?.started) {
      setMoving(true); setOpen(false);
      notify?.("正在移动并重启管理器", "success");
    } else {
      await load(); setOpen(false);
      notify?.(result.message || "应用已在该位置", "success");
    }
  });
  const shortcut = () => operate("shortcut", async () => {
    setShortcutInfo(null);
    const result = await api("/api/application/location/shortcut", { method: "POST", body: "{}", timeoutMs: 30000 });
    if (!result.result?.verified || !result.result?.exists || !result.result?.path) throw new Error("未能确认快捷方式已保存，请重试");
    setShortcutInfo(result.result);
    notify?.(result.message || (result.result.created ? "桌面快捷方式已创建" : "桌面快捷方式已就绪"), "success");
  });
  const unavailable = disabled || Boolean(busy) || moving || !location?.supported;
  const Container = embedded ? "div" : "section";
  return <Container className={embedded ? "setting-row static application-location-cell" : "settings-card full application-location-card"} aria-labelledby="application-location-title">
    {embedded ? <strong id="application-location-title">应用位置</strong>
      : <header><span><FolderOpen size={19} /></span><div><h2 id="application-location-title">应用位置</h2><p>移动程序或创建桌面快捷方式</p></div></header>}
    <div className="application-location-row">
      <button className="application-location-path" title={location?.executable || ""} disabled={unavailable}
        onClick={() => { setDirectory(location?.directory || ""); setError(""); setOpen(true); }}>
        {location?.executable || "正在读取应用位置…"}
      </button>
      <div className="application-location-actions">
        <button className="button secondary compact" disabled={unavailable} onClick={() => { setDirectory(location.directory); setError(""); setOpen(true); }}>
          {moving ? <Loader2 className="spin" size={14} /> : <FolderOpen size={14} />}{moving ? "移动中" : "更改位置"}
        </button>
        <button className="button secondary compact" disabled={unavailable} onClick={shortcut}>
          {busy === "shortcut" ? <Loader2 className="spin" size={14} /> : <Link2 size={14} />}创建桌面快捷方式
        </button>
      </div>
    </div>
    {shortcutInfo && <div className="application-shortcut-result" role="status">
      <span title={shortcutInfo.path}>快捷方式：{shortcutInfo.path}</span>
      <button className="button subtle compact" disabled={Boolean(busy) || moving} onClick={() => operate("reveal", async () => {
        await api("/api/application/location/shortcut/reveal", { method: "POST", body: "{}" });
      })}><FolderOpen size={13} />定位快捷方式</button>
    </div>}
    {!location?.supported && location?.message && <p className="application-location-note">{location.message}</p>}
    {location?.relocation?.state === "cleanup_pending" && <p className="application-location-note" role="status">{location.relocation.message || "正在等待启动确认，原位置文件暂时保留。"}</p>}
    {error && !open && <div className="application-location-feedback" role="status"><span>{error}</span>
      <button className="button subtle compact" disabled={Boolean(busy)} onClick={() => operate("refresh", load)}><RefreshCw size={14} />刷新</button></div>}
    {open && <Modal title="更改应用位置" description="将关闭 Codex、恢复配置，并在新位置启动管理器。账号和会话数据会继续保留。"
      onClose={() => !busy && setOpen(false)} dismissible={!busy} className="application-location-modal">
      <div className="modal-body">
        <label htmlFor="application-location-directory">目标文件夹</label>
        <div className="application-location-input">
          <input id="application-location-directory" value={directory} onChange={event => setDirectory(event.target.value)} disabled={Boolean(busy)} spellCheck={false} />
          {location?.canBrowse && <button className="button secondary compact" disabled={Boolean(busy)} onClick={browse}>
            {busy === "browse" ? <Loader2 className="spin" size={14} /> : <FolderOpen size={14} />}选择文件夹
          </button>}
        </div>
        <p className="application-location-note">新程序启动验证通过后，自动删除原位置的程序文件。</p>
        {error && <p className="application-location-error" role="alert">{error}</p>}
      </div>
      <footer className="modal-actions">
        <button className="button secondary" disabled={Boolean(busy)} onClick={() => setOpen(false)}>取消</button>
        <button className="button primary" disabled={Boolean(busy) || !directory.trim()} onClick={move}>
          {busy === "move" && <Loader2 className="spin" size={14} />}移动并重启
        </button>
      </footer>
    </Modal>}
  </Container>;
}
