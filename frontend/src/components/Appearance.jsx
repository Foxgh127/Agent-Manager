import { createContext, useContext, useEffect, useLayoutEffect, useState } from "react";
import { Monitor, Moon, Sun } from "lucide-react";

const AppearanceContext = createContext(null);
const readPreference = () => {
  try { const value = localStorage.getItem("agent-manager-appearance"); return ["light", "dark", "system"].includes(value) ? value : "system"; }
  catch { return "system"; }
};

export function AppearanceProvider({ children }) {
  const [appearance, setAppearance] = useState(readPreference);
  const [systemDark, setSystemDark] = useState(() => window.matchMedia("(prefers-color-scheme: dark)").matches);
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const update = event => setSystemDark(event.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useLayoutEffect(() => {
    document.documentElement.dataset.theme = appearance === "system" ? (systemDark ? "dark" : "light") : appearance;
    try { localStorage.setItem("agent-manager-appearance", appearance); } catch { /* Session preference still works. */ }
  }, [appearance, systemDark]);
  return <AppearanceContext.Provider value={{ appearance, setAppearance }}>{children}</AppearanceContext.Provider>;
}

export function useSavedAppearance(preference) {
  const { setAppearance } = useContext(AppearanceContext);
  useLayoutEffect(() => {
    if (["light", "dark", "system"].includes(preference)) setAppearance(preference);
  }, [preference, setAppearance]);
}

export function useNativeWindowAppearance(send, ready) {
  useEffect(() => {
    if (!ready) return undefined;
    let stopped = false, pending = false, latest = "", applied = "";
    const sync = async () => {
      const root = document.documentElement;
      latest = JSON.stringify({theme: root.dataset.theme || "dark", workspace: root.dataset.workspace || "home"});
      if (pending || latest === applied) return;
      pending = true;
      try {
        while (!stopped && latest !== applied) {
          const value = latest;
          await send("/api/window/appearance", {method: "POST", body: value});
          applied = value;
        }
      } catch { /* Native caption support does not block the workspace. */ }
      finally { pending = false; }
    };
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme", "data-workspace"]});
    sync();
    return () => { stopped = true; observer.disconnect(); };
  }, [send, ready]);
}

export function AppearanceControls({ onSave }) {
  const { appearance, setAppearance } = useContext(AppearanceContext);
  const [saving, setSaving] = useState(false);
  const selectAppearance = async value => {
    if (saving || value === appearance) return;
    const previous = appearance;
    setAppearance(value);
    setSaving(true);
    try { await onSave?.(value); } catch { setAppearance(previous); }
    finally { setSaving(false); }
  };
  return <section className="settings-card appearance-card full">
    <header><div><h2>外观</h2><p>柔和材质与清晰层次，跟随你的使用环境。</p></div></header>
    <div className="appearance-options" role="group" aria-label="界面外观">
      {[["light", "浅色", Sun], ["dark", "深色", Moon], ["system", "跟随系统", Monitor]].map(([id, label, Icon]) => <button key={id} type="button" disabled={saving} aria-pressed={appearance === id} className={appearance === id ? "selected" : ""} onClick={() => selectAppearance(id)}><Icon size={18} /><span>{label}</span></button>)}
    </div>
  </section>;
}
