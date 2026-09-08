export function relayReconnectTarget(details) {
  try {
    const saved = new URL(details.savedOrigin);
    const current = new URL(details.currentOrigin);
    const target = new URL(details.reconnectUrl);
    const valid = url => url.protocol === "https:" && !url.username && !url.password && (!url.port || url.port === "443");
    if (![saved, current, target].every(valid) || target.origin !== current.origin || saved.origin === current.origin) return "";
    if (saved.hostname.replace(/^www\./, "") !== current.hostname.replace(/^www\./, "")) return "";
    return target.origin;
  } catch { return ""; }
}
