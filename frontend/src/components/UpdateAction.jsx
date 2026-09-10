import { Check, Download, Loader2, RefreshCw } from "lucide-react";

export default function UpdateAction({ busy = "", current = false, disabled = false, label = "检查更新", download = false, onClick }) {
  const Icon = busy ? Loader2 : current ? Check : download ? Download : RefreshCw;
  return <button className={`button secondary compact update-action${busy ? " is-busy" : ""}${current && !busy ? " is-current" : ""}`}
    disabled={Boolean(busy) || current || disabled} aria-busy={Boolean(busy)} onClick={onClick}>
    <Icon size={14} className={busy ? "spin" : ""} />{busy || (current ? "已是最新版" : label)}
  </button>;
}
