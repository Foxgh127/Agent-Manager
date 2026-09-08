import { useEffect, useRef, useState } from "react";
import { ExternalLink, KeyRound, Loader2, RefreshCw } from "lucide-react";
import { Modal } from "./Dialog.jsx";

export default function AccountReauthModal({ account, api, notify, onDone, onClose }) {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState("");
  const [callback, setCallback] = useState("");
  const [busy, setBusy] = useState(false);
  const started = useRef(false);
  const currentId = useRef("");
  const finished = useRef(false);
  const begin = async () => {
    setBusy(true); setError("");
    try {
      const existing = await api("/api/oauth/status");
      if (existing.status.reauthAccountId === account.id && ["starting","waiting","exchanging"].includes(existing.status.status)) {
        currentId.current = existing.status.loginId; setStatus(existing.status); return;
      }
      const response = await api("/api/accounts/" + encodeURIComponent(account.id) + "/reauth", {method:"POST",body:"{}"});
      currentId.current = response.status.loginId;
      setStatus(response.status);
    } catch (cause) { setError(cause.message); }
    finally { setBusy(false); }
  };
  useEffect(() => {
    if (!started.current) { started.current = true; begin(); }
  }, []);
  useEffect(() => {
    if (!["starting","waiting","exchanging"].includes(status?.status)) return;
    let cancelled = false;
    let timer;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const response = await api("/api/oauth/status", {signal:controller.signal});
        if (cancelled) return;
        if (response.status.loginId !== currentId.current) {
          setError("认证会话已改变，请重新开始。"); setStatus(null); return;
        }
        setStatus(response.status);
        if (response.status.status === "completed" && !finished.current) {
          finished.current = true;
          notify("账号已重新认证，额度与模型正在同步");
          await onDone?.(); onClose(); return;
        }
        if (response.status.error) setError(response.status.error);
      } catch (cause) { if (!cancelled && cause.name !== "AbortError") setError(cause.message); }
      if (!cancelled) timer = setTimeout(poll, 1200);
    };
    timer = setTimeout(poll, 700);
    return () => { cancelled = true; clearTimeout(timer); controller.abort(); };
  }, [status?.status, onDone, onClose, notify, api]);
  const action = async (name, data = {}) => {
    setBusy(true); setError("");
    try {
      const response = await api("/api/oauth/" + name, {method:"POST",body:JSON.stringify({loginId:currentId.current,...data})});
      setStatus(response.status);
      if (name === "cancel") onClose();
    } catch (cause) { setError(cause.message); }
    finally { setBusy(false); }
  };
  const active = ["starting","waiting","exchanging"].includes(status?.status);
  return <Modal title="重新认证官方账号" description={account.email || account.label} dismissible={!busy} onClose={active ? () => action("cancel") : onClose}>
    <div className="modal-body form-stack">
      <p><KeyRound size={18}/> 请在官方授权页选择上面的账号。原账号、分组和模型设置会保留。</p>
      {error && <p className="inline-error" role="alert">{error}</p>}
      <p role="status">{busy || status?.status === "exchanging" ? <><Loader2 size={16} className="spin"/> 正在处理认证…</> : active ? "完成网页登录后会自动继续，无需手动点击登录完成。" : "可以重新打开授权流程。"}</p>
      <div className="modal-actions">
        {active ? <button className="button secondary" disabled={busy} onClick={() => action("open")}><ExternalLink size={16}/>打开授权页</button> : <button className="button primary" disabled={busy} onClick={begin}><RefreshCw size={16}/>重新开始</button>}
      </div>
      {active && <details><summary>浏览器未能自动返回？</summary><div className="form-stack">
        <label className="field"><span>粘贴浏览器地址栏中的完整回调地址</span><input value={callback} onChange={event => setCallback(event.target.value)} autoComplete="off" spellCheck={false}/></label>
        <button className="button secondary" disabled={busy || !callback.trim()} onClick={() => action("callback", {callbackUrl:callback.trim()})}>提交回调</button>
      </div></details>}
    </div>
    <footer className="modal-footer"><button className="button secondary" disabled={busy} onClick={active ? () => action("cancel") : onClose}>{active ? "取消认证" : "关闭"}</button></footer>
  </Modal>;
}
