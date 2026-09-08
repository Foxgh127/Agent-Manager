import { Component, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { AppearanceProvider } from "./components/Appearance.jsx";
import "./styles.css";
import "./workspace.css";
import "./theme.css";
import "./workspaceThemes.css";
import "./cardPalette.css";

class ApplicationErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error) {
    // Do not serialize component props or application state here: they may
    // contain private account metadata. The console receives only the bounded
    // React error message for local diagnostics.
    console.error("Agent Manager UI error:", String(error?.message || error).slice(0, 500));
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="fatal-error" role="alert">
        <img src="/app-icon.png" alt="" />
        <span>SAFE RECOVERY</span>
        <h1>界面没有正常完成加载</h1>
        <p>账号、密钥与 Codex 配置均未因此修改。可以重新载入界面；若问题持续，请在设置中的诊断中心执行只读检查。</p>
        <button type="button" onClick={() => window.location.reload()}>重新载入界面</button>
      </main>
    );
  }
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <ApplicationErrorBoundary>
      <AppearanceProvider><App /></AppearanceProvider>
    </ApplicationErrorBoundary>
  </StrictMode>,
);
