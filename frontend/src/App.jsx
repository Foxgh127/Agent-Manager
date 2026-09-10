import { APP_VERSION } from "./version.js";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { Modal, ConfirmDialog } from "./components/Dialog.jsx";
import RecoveryPanel from "./components/RecoveryPanel.jsx";
import ConfigRecoveryPanel from "./components/ConfigRecoveryPanel.jsx";
import AppUpdatePanel from "./components/AppUpdatePanel.jsx";
import RelayOriginNotice from "./components/RelayOriginNotice.jsx";
import SessionSyncModal from "./components/SessionSyncModal.jsx";
import SessionRepairPanel from "./components/SessionRepairPanel.jsx";
import AccountReauthModal from "./components/AccountReauthModal.jsx";
import { quotaIsCurrent } from "./quotaFreshness.js";
import { contextBudget } from "./contextBudget.js";
import { createConfirmationQueue } from "./confirmationQueue.js";
import { usageSourceKey, usageSourceLabels } from "./usageViewModel.js";
import { applyDashboardMoveResult } from "./dashboardMove.js";
import { normalizeUsageRange, resolveUsageRange, filterUsageRecordsByRange, usageRecordDateKey } from "./usageRange.js";
import DiscreteSlider from "./components/DiscreteSlider.jsx";
import useAccountCardDrag from "./components/useAccountCardDrag.js";
import QuotaEstimate from "./components/QuotaEstimate.jsx";
import PoolProtocols from "./components/PoolProtocols.jsx";
import { UsageRangeControls } from "./components/UsageRangeControls.jsx";
import { AppearanceControls, useSavedAppearance, useNativeWindowAppearance } from "./components/Appearance.jsx";
import RefinedSelect from "./components/RefinedSelect.jsx";
import { subscriptionBadge } from "./subscriptionBadge.js";
import "./components/subscriptionBadge.css";
import { listedModels } from "./modelList.js";
import ResetRadarPanel from "./components/ResetRadarPanel.jsx";
import { matchesAccount, visibleSourceIds } from "./accountViewModel.js";
import { accountRefreshNotice, batchRefreshNotice, reconcileModelSelection, runBoundedRefresh } from "./modelRefresh.js";
import { accountRefreshDiagnostics } from "./refreshDiagnostics.js";
import {
  AlertTriangle,
  Archive,
  BarChart3,
  Bot,
  Boxes,
  BookOpen,
  ArrowDown,
  ArrowUp,
  CalendarDays,
  BrainCircuit,
  Check,
  CheckSquare,
  ChevronLeft,
  ChevronDown,
  ChevronRight,
  CircleGauge,
  Clock3,
  Copy,
  Download,
  Eye,
  EyeOff,
  Database,
  ExternalLink,
  Gauge,
  Globe2,
  GripVertical,
  HardDriveDownload,
  Info,
  KeyRound,
  Layers3,
  ListOrdered,
  Loader2,
  LogOut,
  Mail,
  MessageSquareText,
  Pencil,
  Pin,
  Play,
  PlugZap,
  Plus,
  RefreshCw,
  Route,
  RotateCcw,
  Save,
  Search,
  Server,
  Settings,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Square,
  Trash2,
  Upload,
  UserPlus,
  Users,
  Wrench,
  X,
  Zap,
} from "lucide-react";

function consumeBootstrapToken() {
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const token = fragment.get("bootstrap") || "";
  if (window.location.hash) {
    try {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    } catch {
      // The fragment remains local to this document even when history APIs are
      // unavailable; browsers never include it in the HTTP request target.
    }
  }
  return token;
}

const bootstrapToken = consumeBootstrapToken();
let apiToken = "";
try {
  // A fresh app launch always carries a new one-time bootstrap. Never let an
  // old token for a coincidentally reused loopback port bypass that exchange.
  if (bootstrapToken) {
    window.sessionStorage.removeItem("agent-manager-token");
  } else {
    apiToken = window.sessionStorage.getItem("agent-manager-token") || "";
  }
} catch {
  // A restricted WebView can still use the in-memory token after exchange.
}
let apiTokenPromise = null;

async function ensureApiToken() {
  if (apiToken) return apiToken;
  if (!apiTokenPromise) {
    apiTokenPromise = (async () => {
      let lastNetworkError = null;
      for (let attempt = 0; attempt < 3; attempt += 1) {
        try {
          const response = await fetch("/api/session/bootstrap", {
            method: "POST",
            headers: { "X-Agent-Manager-Bootstrap": bootstrapToken },
            cache: "no-store",
          });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok || !payload.token) {
            const rejected = new Error(payload.error || "管理界面会话已过期，请重新打开 Agent Manager。");
            rejected.bootstrapRejected = true;
            throw rejected;
          }
          apiToken = payload.token;
          try {
            window.sessionStorage.setItem("agent-manager-token", apiToken);
          } catch {
            // Keep the token in memory when WebView storage is unavailable.
          }
          return apiToken;
        } catch (error) {
          if (error?.bootstrapRejected) throw error;
          lastNetworkError = error;
          if (attempt < 2) {
            await new Promise((resolve) => window.setTimeout(resolve, 150 * (attempt + 1)));
          }
        }
      }
      throw lastNetworkError || new Error("无法连接本地管理服务，请重新打开 Agent Manager。");
    })()
      .catch((error) => {
        apiTokenPromise = null;
        throw error;
      });
  }
  return apiTokenPromise;
}
const MAX_IMPORT_FILES = 500;
const MAX_IMPORT_BYTES = 24_000_000;
const IMPORT_PREVIEW_PAGE = 200;
const OAUTH_MAIL_POLL_MS = 20_000;
const OAUTH_MAIL_MAX_AUTOMATIC_CHECKS = 12;
const READ_REQUEST_TIMEOUT_MS = 30_000;
const WRITE_REQUEST_TIMEOUT_MS = 120_000;
let oauthLoginHelperDraft = "";
let oauthLoginHelperSessionId = "";
let skillsResourceCache = null;
let maintenanceResourceCache = null;
let usageResourceCache = null;
let radarResourceCache = { loaded: false, payload: null, error: "" };
let startupResourceRefreshPromise = null;
let runtimeStatusResourceCache = { loaded: false, value: null };
const levels = ["simple", "normal", "hard", "expert"];
const levelLabels = {
  simple: "简单",
  normal: "普通",
  hard: "困难",
  expert: "专家",
};
const levelHints = {
  simple: "机械修改、检索、单文件任务",
  normal: "局部实现与针对性测试",
  hard: "跨模块修改与复杂调试",
  expert: "架构、安全与独立复核",
};
const effortLabels = {
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "超高",
  max: "最大",
  ultra: "Ultra",
};

let runtimeRepairPromptOpen = false;

function isSelectableImportPreviewItem(item) {
  // Existing accounts are valid update targets. Only invalid rows and a
  // duplicate later in the same batch must be disabled.
  return Boolean(item?.valid && !item?.duplicateInBatch);
}

function selectableImportPreviewIndices(items) {
  return new Set(
    (Array.isArray(items) ? items : [])
      .filter(isSelectableImportPreviewItem)
      .map((item) => Number(item.index))
      .filter((index) => Number.isInteger(index) && index >= 0),
  );
}

function showCodexRuntimeRepairPrompt(errorMessage) {
  if (runtimeRepairPromptOpen) return;
  runtimeRepairPromptOpen = true;
  const overlay = document.createElement("div");
  Object.assign(overlay.style, {
    position: "fixed",
    inset: "0",
    zIndex: "10000",
    display: "grid",
    placeItems: "center",
    padding: "24px",
    background: "rgba(2, 8, 18, .76)",
    backdropFilter: "blur(8px)",
  });
  const panel = document.createElement("section");
  Object.assign(panel.style, {
    width: "min(560px, 100%)",
    border: "1px solid rgba(64, 220, 185, .35)",
    borderRadius: "20px",
    padding: "24px",
    color: "#e7f4f0",
    background: "#101c1a",
    boxShadow: "0 24px 80px rgba(0,0,0,.5)",
    fontFamily: "inherit",
  });
  const title = document.createElement("h2");
  title.textContent = "需要修复 Codex 运行时";
  Object.assign(title.style, { margin: "0 0 10px", fontSize: "21px" });
  const message = document.createElement("p");
  message.textContent = errorMessage;
  Object.assign(message.style, { margin: "0 0 12px", color: "#b7c9c4", lineHeight: "1.65" });
  const detail = document.createElement("p");
  detail.textContent =
    "将先扫描 Microsoft Store、新版 ChatGPT.exe、正在运行的 Codex 和桌面端内置运行时。仍未找到时会复用 npm 或部署经校验的官方 Windows 运行时；不会把 codex.cmd 写入 CODEX_CLI_PATH，桌面端始终使用自带的原生 codex.exe。";
  Object.assign(detail.style, {
    margin: "0 0 20px",
    padding: "12px 14px",
    borderRadius: "12px",
    color: "#8fa9a2",
    background: "rgba(255,255,255,.035)",
    lineHeight: "1.6",
    fontSize: "13px",
  });
  const actions = document.createElement("div");
  Object.assign(actions.style, { display: "flex", justifyContent: "flex-end", gap: "10px" });
  const close = document.createElement("button");
  close.textContent = "稍后处理";
  const repair = document.createElement("button");
  repair.textContent = "一键扫描并部署";
  for (const button of [close, repair]) {
    Object.assign(button.style, {
      minHeight: "44px",
      padding: "0 16px",
      borderRadius: "11px",
      border: "1px solid rgba(255,255,255,.13)",
      color: "#e7f4f0",
      background: "rgba(255,255,255,.06)",
      cursor: "pointer",
      font: "inherit",
    });
  }
  Object.assign(repair.style, {
    borderColor: "rgba(49, 218, 177, .5)",
    background: "linear-gradient(135deg, #167b67, #1b9a83)",
  });
  const dismiss = () => {
    overlay.remove();
    runtimeRepairPromptOpen = false;
  };
  close.addEventListener("click", dismiss);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay && !repair.disabled) dismiss();
  });
  repair.addEventListener("click", async () => {
    repair.disabled = true;
    close.disabled = true;
    repair.textContent = "正在扫描与部署…";
    detail.textContent = "正在处理，请不要重复点击或关闭管理器；下载安装可能需要几分钟。";
    try {
      const payload = await api("/api/codex-runtime/deploy", {
        method: "POST",
        body: "{}",
        timeoutMs: 600_000,
      });
      title.textContent = "Codex 运行时已就绪";
      message.textContent = payload.message || "已完成扫描与修复。";
      detail.textContent =
        "现在可以直接在 Codex 桌面端应用账号切换、模型聚合和子代理调度；无需打开终端，也无需重启电脑。";
      const done = repair.cloneNode(false);
      done.textContent = "完成";
      done.disabled = false;
      done.addEventListener("click", () => window.location.reload());
      repair.replaceWith(done);
    } catch (error) {
      title.textContent = "自动修复未完成";
      message.textContent = error.message;
      detail.textContent =
        "没有修改账号数据。再次点击会重新扫描桌面端、npm、PATH，并尝试经过 SHA-512 校验的官方独立运行时；不会重复安装已存在的组件。";
      repair.textContent = "重试";
      repair.disabled = false;
      close.disabled = false;
    }
  });
  actions.append(close, repair);
  panel.append(title, message, detail, actions);
  overlay.append(panel);
  document.body.append(overlay);
}

async function api(path, options = {}) {
  const sessionToken = await ensureApiToken();
  const method = String(options.method || "GET").toUpperCase();
  const timeoutMs = Number.isFinite(options.timeoutMs)
    ? Math.max(1_000, Number(options.timeoutMs))
    : method === "GET"
      ? READ_REQUEST_TIMEOUT_MS
      : WRITE_REQUEST_TIMEOUT_MS;
  const timeoutController = new AbortController();
  const externalSignal = options.signal;
  let timedOut = false;
  const abortFromExternal = () => timeoutController.abort(externalSignal?.reason);
  if (externalSignal?.aborted) abortFromExternal();
  else externalSignal?.addEventListener("abort", abortFromExternal, { once: true });
  const timer = window.setTimeout(() => {
    timedOut = true;
    timeoutController.abort();
  }, timeoutMs);
  try {
    const fetchOptions = { ...options };
    delete fetchOptions.timeoutMs;
    delete fetchOptions.signal;
    const response = await fetch(path, {
      cache: "no-store",
      ...fetchOptions,
      signal: timeoutController.signal,
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Manager-Token": sessionToken,
        ...(options.headers || {}),
      },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) {
      const message = payload.error || `请求失败（${response.status}）`;
      if (message.includes("未找到可用的 Codex 运行时"))
        showCodexRuntimeRepairPrompt(message);
      throw new Error(message);
    }
    return payload;
  } catch (error) {
    if (timedOut) {
      const seconds = Math.ceil(timeoutMs / 1000);
      if (method === "GET")
        throw new Error(`读取超过 ${seconds} 秒仍未返回，已停止等待。请检查网络后手动刷新。`);
      throw new Error(
        `操作超过 ${seconds} 秒仍未返回，界面已停止等待；后台可能仍在收尾，请先刷新状态确认，避免重复提交。`,
      );
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
    externalSignal?.removeEventListener("abort", abortFromExternal);
  }
}

function refreshStartupResourcesOnce() {
  if (startupResourceRefreshPromise) return startupResourceRefreshPromise;
  startupResourceRefreshPromise = Promise.allSettled([
    api("/api/radar"),
    api("/api/updates"),
    api("/api/emergency/checks"),
  ]).then(async ([radar, updates, checks]) => {
    if (radar.status === "fulfilled") {
      radarResourceCache = { loaded: true, payload: radar.value, error: "" };
    } else {
      radarResourceCache = {
        loaded: true,
        payload: radarResourceCache.payload,
        error: radar.reason?.message || "雷达首次读取失败。",
      };
    }
    const maintenanceErrors = [updates, checks]
      .filter((item) => item.status === "rejected")
      .map((item) => item.reason?.message)
      .filter(Boolean);
    maintenanceResourceCache = {
      updates: updates.status === "fulfilled" ? updates.value : null,
      checks: checks.status === "fulfilled" ? checks.value : null,
      error: maintenanceErrors.join("；"),
    };
    // Startup reads persistent inventories. Full discovery and remote catalog
    // refresh remain explicit actions on the corresponding management page.
    const [installed, catalog] = await Promise.allSettled([
      api("/api/skills"),
      api("/api/skills/catalog"),
    ]);
    const skillErrors = [installed, catalog]
      .filter((item) => item.status === "rejected")
      .map((item) => item.reason?.message)
      .filter(Boolean);
    skillsResourceCache = {
      installedPayload: installed.status === "fulfilled" ? installed.value : null,
      catalogPayload: catalog.status === "fulfilled" ? catalog.value : null,
      error: skillErrors.join("；"),
    };
    return { maintenanceResourceCache, skillsResourceCache };
  });
  return startupResourceRefreshPromise;
}

function cx(...names) {
  return names.filter(Boolean).join(" ");
}

function formatTime(value) {
  if (!value) return "尚未刷新";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "尚未刷新";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatDateTime(value) {
  if (!value) return "未提供";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未提供";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function remainingDuration(value) {
  if (!value) return { known: false, label: "待同步", expired: false };
  const date = new Date(value);
  if (Number.isNaN(date.getTime()))
    return { known: false, label: "待同步", expired: false };
  const milliseconds = date.getTime() - Date.now();
  if (milliseconds <= 0) return { known: true, label: "已到期", expired: true };
  const minutes = Math.max(1, Math.ceil(milliseconds / 60_000));
  if (minutes >= 1_440)
    return {
      known: true,
      label: `${Math.ceil(minutes / 1_440)}天`,
      expired: false,
    };
  if (minutes >= 60)
    return {
      known: true,
      label: `${Math.ceil(minutes / 60)}小时`,
      expired: false,
    };
  return { known: true, label: `${minutes}分钟`, expired: false };
}

function formatBalance(balance) {
  if (balance?.unlimited) return "不限额度";
  const rawAmount = typeof balance?.amount === "number"
    ? balance.amount
    : typeof balance?.remaining === "number"
      ? balance.remaining
      : null;
  if (rawAmount == null) return "未提供";
  const amount = new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 4,
  }).format(rawAmount);
  return balance.currency ? `${balance.currency} ${amount}` : `${amount} 额度`;
}

function formatBalanceAmount(value, balance) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "未提供";
  const amount = new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 4,
  }).format(value);
  return balance?.currency ? `${balance.currency} ${amount}` : `${amount} 额度`;
}

function providerIntegrationLabel(value) {
  const kind = String(value?.integrationKind || value || "").toLowerCase();
  if (kind === "new_api") return "New API";
  if (kind === "sub2api") return "Sub2API";
  return "OpenAI 兼容";
}

function relayPlatformKind(platform, name = "") {
  const value = `${platform || ""} ${name || ""}`.toLowerCase();
  if (/anthropic|claude|kiro/.test(value)) return "claude";
  if (/openai|codex|gpt/.test(value)) return "codex";
  return "other";
}

function isCodexRelayRecord(item) {
  const models = Array.isArray(item?.models) ? item.models.join(" ") : "";
  const value = `${item?.platform || ""} ${item?.groupPlatform || ""} ${item?.name || ""} ${item?.group || ""} ${models}`.toLowerCase();
  return !/anthropic|claude|kiro|midjourney|dall-?e|gpt-image|stable[- ]diffusion|flux|imagen|veo|sora|image[- ]generation|生图|绘图|画图/.test(value);
}

function isCodexRelayEndpoint(item) {
  const value = `${item?.name || ""} ${item?.baseUrl || ""} ${item?.description || ""} ${(item?.aliases || []).join(" ")}`.toLowerCase();
  return !/image\.|\/images|image[- ]generation|midjourney|dall-?e|生图|绘图|画图/.test(value);
}

function formatRelayRate(value) {
  return typeof value === "number" && Number.isFinite(value)
    ? `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 }).format(value)}×`
    : "倍率未提供";
}

function relayRateDetail(group) {
  const effective = formatRelayRate(group?.rateMultiplier ?? group?.groupRateMultiplier);
  const base = group?.baseRateMultiplier;
  if (typeof base === "number" && typeof group?.rateMultiplier === "number" && base !== group.rateMultiplier)
    return `计费倍率 ${effective}（基础 ${formatRelayRate(base)}）`;
  return `计费倍率 ${effective}`;
}

function isFreePlan(...values) {
  return values.some((value) => {
    const normalized = String(value || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "");
    return ["free", "chatgptfree", "freeplan", "chatgptfreeplan"].includes(
      normalized,
    );
  });
}

function SubscriptionBadge({ label, rawPlan, usagePlan }) {
  const badge = subscriptionBadge(
    [label, rawPlan, usagePlan], false,
  );
  return (
    <span
      className={cx("plan-badge", `tier-${badge.tier}`)}
      title={`ChatGPT 套餐：${badge.label}`}
    >
      {badge.tier === "free" ? (
        <ShieldCheck size={12} />
      ) : (
        <Sparkles size={12} />
      )}
      <span>{badge.label}</span>
    </span>
  );
}

function endpointHost(value) {
  try {
    return new URL(String(value || "")).host || "未配置";
  } catch {
    return String(value || "").replace(/^https?:\/\//i, "").split("/")[0] || "未配置";
  }
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(
    units.length - 1,
    Math.floor(Math.log(bytes) / Math.log(1024)),
  );
  const amount = bytes / 1024 ** index;
  return `${amount >= 10 || index === 0 ? Math.round(amount) : amount.toFixed(1)} ${units[index]}`;
}

async function copyText(value) {
  const text = String(value || "");
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Some WebView2 policies expose Clipboard but reject writes. Continue
      // with the selection-based fallback inside the active dialog scope.
    }
  }
  const previousFocus = document.activeElement;
  const input = document.createElement("textarea");
  input.value = text;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.opacity = "0";
  const container = previousFocus?.closest?.('[role="dialog"], [role="alertdialog"]') || document.body;
  container.appendChild(input);
  let copied = false;
  try {
    input.select();
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  } finally {
    input.remove();
    if (previousFocus?.isConnected) previousFocus.focus?.({ preventScroll: true });
  }
  if (!copied) throw new Error("系统剪贴板不可用，请在文本框中手动复制");
}

function IconButton({ label, children, className = "", ...props }) {
  return (
    <button
      type="button"
      className={cx("icon-button", className)}
      aria-label={label}
      title={label}
      {...props}
    >
      {children}
    </button>
  );
}

function Switch({ checked, onChange, label, ariaLabel, disabled = false }) {
  return (
    <label className="switch-control">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-label={ariaLabel || label}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="switch-track">
        <span />
      </span>
      {label && <span>{label}</span>}
    </label>
  );
}

function AnimatedSize({ children, className = "" }) {
  const contentRef = useRef(null);
  const [height, setHeight] = useState(null);

  useEffect(() => {
    const content = contentRef.current;
    if (!content) return undefined;
    const measure = () => setHeight(content.getBoundingClientRect().height);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(content);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      className={cx("animated-size", className)}
      style={height == null ? undefined : { height: `${height}px` }}
    >
      <div ref={contentRef}>{children}</div>
    </div>
  );
}

function groupedSources(sources) {
  const groups = [];
  const byId = new Map();
  sources.forEach((source) => {
    const id = source.groupId || "ungrouped";
    if (!byId.has(id)) {
      const group = { id, name: source.groupName || "未分组", sources: [] };
      byId.set(id, group);
      groups.push(group);
    }
    byId.get(id).sources.push(source);
  });
  return groups;
}

function breadcrumbForModel(sources, key) {
  for (const source of sources) {
    const model = source.models.find((item) => item.key === key);
    if (model) {
      const group = (source.groupName || "未分组").replace(/账号$/, "");
      return `${group}/${source.name}/${model.name}`;
    }
  }
  return "";
}

function Toast({ toast, onClose }) {
  if (!toast) return null;
  return (
    <div className={cx("toast", toast.kind || "success")} role="status">
      {toast.kind === "error" || toast.kind === "warning" ? (
        <AlertTriangle size={18} />
      ) : (
        <Check size={18} />
      )}
      <span>{toast.message}</span>
      <button onClick={onClose} aria-label="关闭通知">
        <X size={16} />
      </button>
    </div>
  );
}

function EmptyState({ group }) {
  return (
    <div className="empty-state">
      <span>
        <Users size={28} />
      </span>
      <h3>{group === "all" ? "还没有导入账号" : "这个分组还没有账号"}</h3>
      <p>
        点击右上角“添加账号”，支持 OAuth、API Key、批量 JSON 和 Web Session。
      </p>
    </div>
  );
}

function QuotaBar({ label, quota, unavailable = false }) {
  if (!quotaIsCurrent(quota, unavailable)) {
    return (
      <div className="quota-row muted">
        <span>
          <strong>{label}</strong>
          <small>{quota?.remainingPercent != null ? "上次额度已过期或尚未重新验证" : "等待同步官方额度"}</small>
        </span>
        <b>待刷新</b>
      </div>
    );
  }
  const remaining = Math.max(
    0,
    Math.min(100, Number(quota.remainingPercent) || 0),
  );
  const quotaColor = remaining <= 15 ? "var(--danger)" : remaining <= 35 ? "var(--amber)" : "var(--accent)";
  return (
    <div className="quota-row" style={{ "--quota-color": quotaColor }}>
      <span>
        <strong>{label}</strong>
        <small>
          {quota.stale
            ? `上次有效额度${quota.resetAt ? ` · ${formatTime(quota.resetAt)} 重置` : ""}`
            : quota.resetAt
              ? `${formatTime(quota.resetAt)} 重置`
              : "未提供重置时间"}
        </small>
      </span>
      <b>{Math.round(remaining)}%</b>
      <progress aria-label={label} max="100" value={remaining} />
    </div>
  );
}

function ResetCreditModal({ item, onClose, onConsume, busy }) {
  const resetCredits = item.record.usage?.resetCredits || {
    availableCount: 0,
    credits: [],
  };
  const credits = resetCredits.credits || [];
  const nextExpiry = credits
    .map((credit) => credit.expiresAt)
    .filter(Boolean)
    .sort()[0];
  return (
    <Modal
      title="官方额度重置卡"
      description={`${item.name} · 发放时间与过期时间分别列出`}
      onClose={onClose}
    >
      <div className="reset-credit-hero">
        <span>
          <RotateCcw size={27} />
        </span>
        <div>
          <small>当前可用</small>
          <strong>{resetCredits.availableCount}</strong>
          <p>
            {nextExpiry
              ? `最近过期时间：${formatDateTime(nextExpiry)}`
              : "官方未返回具体过期时间"}
          </p>
        </div>
      </div>
      <div className="reset-credit-list">
        {credits.map((credit, index) => (
          <article className="reset-credit-item" key={credit.id || index}>
            <span className="status-pill success">可用</span>
            <div>
              <strong>{credit.title || "Codex 完整额度重置"}</strong>
              <small>{credit.description || "可重置当前 Codex 使用额度"}</small>
            </div>
            <dl className="reset-credit-dates">
              <div className="issued">
                <dt>
                  <CalendarDays size={14} />
                  发放时间
                </dt>
                <dd>{formatDateTime(credit.grantedAt)}</dd>
              </div>
              <div className="expires">
                <dt>
                  <Clock3 size={14} />
                  过期时间
                </dt>
                <dd>{formatDateTime(credit.expiresAt)}</dd>
              </div>
            </dl>
          </article>
        ))}
        {!credits.length && resetCredits.availableCount > 0 && (
          <div className="reset-credit-no-detail">
            <Clock3 size={18} />
            <span>官方只返回了可用数量，暂未提供每张卡的发放与到期明细。</span>
          </div>
        )}
        {!credits.length && resetCredits.availableCount <= 0 && (
          <div className="reset-credit-no-detail">
            <Clock3 size={18} />
            <span>
              当前没有可用的官方重置卡。成功邀请或官方活动发放后，会在这里显示数量与到期时间。
            </span>
          </div>
        )}
      </div>
      <div className="modal-footer reset-credit-footer">
        <p>
          <AlertTriangle size={15} />
          使用后会立即消耗 1 张官方重置卡；若当前额度无需重置，官方不会扣卡。
        </p>
        <button
          className="button primary"
          disabled={busy || resetCredits.availableCount <= 0}
          onClick={onConsume}
        >
          {busy ? (
            <Loader2 className="spin" size={17} />
          ) : (
            <RotateCcw size={17} />
          )}
          使用 1 张重置卡
        </button>
      </div>
    </Modal>
  );
}

function ModelListModal({ source, account, onClose, notify, onSaved }) {
  const [query, setQuery] = useState("");
  const [shown, setShown] = useState(100);
  const [editing, setEditing] = useState(null);
  const [saving, setSaving] = useState(false);
  const models = listedModels(account && !account.selectedKeyId ? [] : source?.models);
  const records = new Map((source?.models || []).filter(item => typeof item === "object").map(item => [item.id,item]));
  const filtered = models.filter(model => `${model.id} ${model.label}`.toLowerCase().includes(query.trim().toLowerCase()));
  const key = account?.keys?.find(item => String(item.id) === String(account.selectedKeyId));
  const warning = account && !account.selectedKeyId ? "当前没有可用 Key，请添加或重新导入。" : source?.available === false ? "连接当前不可用，下方保留上次同步的模型。" : source?.modelsError || source?.modelsStale ? "模型目录尚未完成最新同步，下方保留已读取的模型。" : "";
  const copyModels = async text => { try { await copyText(text); notify("已复制模型 ID"); } catch { notify("复制失败，请手动复制模型 ID", "error"); } };
  const editModel = model => {
    const record = records.get(model.id) || {};
    setEditing({ id:model.id, mode:record.reasoningSource === "custom" ? record.reasoningSupported === false ? "disabled" : "custom" : "auto", efforts:record.efforts?.length ? record.efforts : ["low","medium","high","xhigh"], defaultEffort:record.defaultEffort || "" });
  };
  const save = async () => {
    setSaving(true);
    try {
      const response = await api(`/api/providers/${encodeURIComponent(source.recordId)}/model-reasoning`, { method:"POST", body:JSON.stringify({modelId:editing.id,mode:editing.mode,efforts:editing.efforts,defaultEffort:editing.defaultEffort}) });
      await onSaved?.();
      setEditing(null);
      notify(response.result?.applied ? "已更新模型目录；重新打开 Codex 后加载新档位" : "模型设置已保存，将在使用该来源时生效");
    } catch (error) { notify(error.message,"error"); }
    finally { setSaving(false); }
  };
  return <><Modal title={`${source?.name || account?.siteName || "账号"} · 可用模型`} description={`${models.length} 个模型${key?.name ? ` · 当前 Key：${key.name}` : ""}`} onClose={onClose} className="model-list-modal">
    <div className="modal-body model-list-panel">
      {warning && <p className="model-list-notice"><Info size={16} />{warning}</p>}
      {models.length > 0 && <label className="model-list-search"><Search size={17} /><input aria-label="搜索可用模型" placeholder="搜索模型 ID 或名称" value={query} onChange={event => {setQuery(event.target.value);setShown(100);}} /></label>}
      <ul className="available-model-list">
        {filtered.slice(0,shown).map(model => {
          const record=records.get(model.id) || {};
          return <li key={model.id}><span><code>{model.id}</code>{model.label !== model.id && <small>{model.label}</small>}
            <small className="model-effort-summary">{record.efforts?.length ? record.efforts.map(effort => effortLabels[effort] || effort).join(" · ") : "由模型使用默认强度"}{record.reasoningSource === "codex_compatibility" ? " · Codex 兼容档位" : record.reasoningSource === "custom" ? " · 自定义" : ""}</small>
          </span><div className="model-list-actions">{source?.kind === "provider" && <IconButton label={`配置 ${model.id} 的思考强度`} onClick={() => editModel(model)}><Settings size={15} /></IconButton>}<IconButton label={`复制模型 ${model.id}`} onClick={() => copyModels(model.id)}><Copy size={15} /></IconButton></div></li>;
        })}
      </ul>
      {!filtered.length && <p className="model-list-empty">{models.length ? "没有匹配的模型" : account && !account.selectedKeyId ? "添加 Key 后，即可在这里查看模型。" : "尚未读取到模型，请在账号卡片上点击刷新。"}</p>}
      {filtered.length > shown && <button className="button secondary" onClick={() => setShown(value => value + 100)}>显示更多</button>}
    </div>
    <footer className="modal-footer"><span>{query ? `匹配 ${filtered.length} 项` : "来自最近一次模型同步"}</span><button className="button secondary" disabled={!filtered.length} onClick={() => copyModels(filtered.map(item => item.id).join("\n"))}><Copy size={15} />复制列表</button><button className="button primary" onClick={onClose}>完成</button></footer>
  </Modal>{editing && <Modal title="模型思考强度" description={editing.id} onClose={() => !saving && setEditing(null)} className="model-reasoning-modal">
    <div className="model-reasoning-content"><p>优先使用接口提供的范围；缺少范围时，标准 GPT 模型使用本机 Codex 的兼容档位。非标准模型可自行配置。</p>
      <RefinedSelect ariaLabel="模型思考强度设置方式" variant="field" value={editing.mode} options={[{value:"auto",label:"自动识别"},{value:"custom",label:"自定义档位"},{value:"disabled",label:"仅使用默认档位"}]} onChange={mode => setEditing(current => ({...current,mode}))} disabled={saving} />
      {editing.mode === "custom" && <><fieldset className="model-effort-options"><legend>可选思考强度</legend>{["low","medium","high","xhigh","max","ultra"].map(effort => <label key={effort}><input type="checkbox" checked={editing.efforts.includes(effort)} disabled={saving} onChange={event => setEditing(current => {const efforts=event.target.checked ? [...current.efforts,effort] : current.efforts.filter(value => value !== effort);return {...current,efforts,defaultEffort:efforts.includes(current.defaultEffort) ? current.defaultEffort : ""};})} /><span>{effortLabels[effort] || effort}</span></label>)}</fieldset>
        <label className="field"><span>默认强度</span><RefinedSelect ariaLabel="模型默认思考强度" variant="field" value={editing.defaultEffort} options={[{value:"",label:"自动"},...editing.efforts.map(value => ({value,label:effortLabels[value] || value}))]} onChange={defaultEffort => setEditing(current => ({...current,defaultEffort}))} disabled={saving} /></label></>}
      <p className="form-note">自定义范围不会被模型刷新覆盖。实际请求仍需接口支持，旧版 Codex 会过滤尚不支持的 Max / Ultra。</p>
    </div><footer className="modal-footer"><button className="button secondary" disabled={saving} onClick={() => setEditing(null)}>取消</button><button className="button primary" disabled={saving || editing.mode === "custom" && !editing.efforts.length} onClick={save}>{saving ? "正在保存…" : "保存"}</button></footer>
  </Modal>}</>;
}

function RelayAccountCard({
  account,
  source,
  groups,
  onSelect,
  onSelection,
  onGroup,
  onManage,
  onRefresh,
  onExport,
  onDelete,
  onProxy,
  onPortal,
  onModels,
  syncToApi,
  busy,
  selectionMode = false,
  selected = false,
  onToggle,
}) {
  const selectedKey = (account.keys || []).find(
    (item) => String(item.id) === String(account.selectedKeyId),
  );
  const active = Boolean(source?.active);
  const activationRequired = Boolean(source?.activationRequired);
  const configuredKeys = (account.keys || []).filter(
    (item) => item.secretConfigured && isCodexRelayRecord(item),
  );
  const codexGroups = (account.groups || [])
    .filter((item) => item.active !== false && isCodexRelayRecord(item))
    .slice()
    .sort((left, right) => {
      const leftRate = Number(left.rateMultiplier);
      const rightRate = Number(right.rateMultiplier);
      const leftValue = Number.isFinite(leftRate) ? leftRate : Number.POSITIVE_INFINITY;
      const rightValue = Number.isFinite(rightRate) ? rightRate : Number.POSITIVE_INFINITY;
      return (
        leftValue - rightValue ||
        String(left.name || "").localeCompare(String(right.name || ""), "zh-CN")
      );
    });
  const selectedGroup = codexGroups.find(
    (item) => String(item.id) === String(selectedKey?.groupId),
  );
  const remoteKeyCount = Math.max(
    configuredKeys.length,
    Number(account.remoteKeyCount) || 0,
  );
  const localGroup = groups.find(
    (item) => String(item.id) === String(account.groupId || source?.groupId),
  );
  return (
    <article
      data-card-id={"relay:" + account.id}
      data-card-group-id={account.groupId || source?.groupId || "relay"}
      className={cx(
        "account-card account-type-relay relay-account-card service-account-card",
        `card-tone-${localGroup?.color || source?.group?.color || "slate"}`,
        active && "active",
        activationRequired && "activation-required",
        selected && "selected",
      )}
    >
      <header className="account-card-head relay-account-head">
        {selectionMode && source && <button type="button" className={cx("card-checkbox", selected && "checked")} onClick={() => onToggle(source)} aria-pressed={selected} aria-label={`${selected ? "取消选择" : "选择"} ${account.name || account.siteName || "中转站账号"}`}>{selected ? <CheckSquare size={20} /> : <Square size={20} />}</button>}
        <span className="account-avatar relay-account-avatar"><Globe2 size={23} /></span>
        <span className="account-title">
          <span>
            <strong title={account.name || account.siteName || "中转站账号"}>{account.name || account.siteName || "中转站账号"}</strong>
            {configuredKeys.length ? <RefinedSelect variant="key" value={account.selectedKeyId || ""} disabled={busy}
              onChange={keyId => onSelection(account, { keyId })} ariaLabel={`${account.name || account.siteName || "中转站账号"} 当前 Key`} placeholder="选择 Key"
              options={configuredKeys.map(item => ({value:String(item.id),label:item.name || "未命名 Key",detail:item.group || ""}))}
            /> : <button className="relay-missing-key" onClick={() => onManage(account)} disabled={busy}>添加 Key</button>}
            {active && (
              <em
                className={activationRequired ? "warning" : undefined}
                title={activationRequired ? "API Key 或 API 线路已变更，需要重新应用到 Codex" : "当前使用"}
              >
                {activationRequired ? <AlertTriangle size={12} /> : <Check size={12} />}
                {activationRequired ? "配置待应用" : "当前使用"}
              </em>
            )}
          </span>
          <small className="provider-address">
            <span>{account.portalUrl || account.origin}</span>
            {(account.portalUrl || account.origin) && (
              <button
                type="button"
                onClick={() => onPortal(source)}
                disabled={busy || !source}
                aria-label={`打开 ${account.siteName || "中转站"} 网站`}
                title="用默认浏览器打开中转站网站"
              >
                <ExternalLink size={13} />
              </button>
            )}
          </small>
        </span>
        <button
          className={cx("play-source", active && !activationRequired && "active")}
          type="button"
          onClick={() => onSelect(account, source)}
          disabled={busy || !source?.available || !account.selectedKeyId}
          title={activationRequired ? "重新应用新选择的 Key / 端点并重启 Codex" : "使用这个中转站账号"}
        >
          {active && !activationRequired ? <Check size={20} /> : activationRequired ? <RefreshCw size={19} /> : <Play size={19} fill="currentColor" />}
        </button>
      </header>

      <div className="account-tags relay-account-tags">
        <RefinedSelect variant="group" value={account.groupId || source?.groupId || ""}
          ariaLabel={`${account.name || account.siteName || "中转站账号"} 本机分组`} disabled={busy}
          onChange={groupId => onGroup(account, source, groupId)}
          options={groups.map(group => ({value:group.id,label:group.name,tone:group.color}))} />
        <span
          className={cx(
            "login-method-badge web-login",
            !account.dashboardSessionStored && "attention",
          )}
          title={
            account.dashboardSessionStored
              ? "网页登录凭据已在本机加密保存"
              : "刷新时会自动打开网页登录窗口"
          }
        >
          <Globe2 size={12} />网页登录
        </span>
      </div>

      <div className="relay-route-control">
        <span>当前线路</span>
        <RefinedSelect variant="route" value={selectedKey?.groupId || ""} disabled={busy || !selectedKey || !codexGroups.length}
          onChange={groupId => onSelection(account, { groupId })} ariaLabel={`${account.name || account.siteName || "中转站账号"} 当前 Key 分组`}
          placeholder={selectedKey ? selectedKey.group || "默认线路" : "先选择 Key"}
          options={codexGroups.map(item => ({value:String(item.id),label:item.name,detail:formatRelayRate(item.rateMultiplier)}))}
        />
      </div>
      {selectedGroup && [selectedGroup.dailyLimitUsd, selectedGroup.weeklyLimitUsd, selectedGroup.monthlyLimitUsd].some(value => typeof value === "number" && value > 0) && <div className="relay-route-limits">
        {typeof selectedGroup.dailyLimitUsd === "number" && selectedGroup.dailyLimitUsd > 0 && <span>日限 {formatBalanceAmount(selectedGroup.dailyLimitUsd, { currency: "USD" })}</span>}
        {typeof selectedGroup.weeklyLimitUsd === "number" && selectedGroup.weeklyLimitUsd > 0 && <span>周限 {formatBalanceAmount(selectedGroup.weeklyLimitUsd, { currency: "USD" })}</span>}
        {typeof selectedGroup.monthlyLimitUsd === "number" && selectedGroup.monthlyLimitUsd > 0 && <span>月限 {formatBalanceAmount(selectedGroup.monthlyLimitUsd, { currency: "USD" })}</span>}
      </div>}

      <dl className="relay-account-metrics" aria-label={`${account.siteName || "中转站"} 账号信息`}>
        <div><dt>账号余额{account.balanceFreshness !== "fresh" && <small className="balance-stale"> · 待刷新</small>}</dt><dd title={account.balanceFreshness === "fresh" ? formatBalance(account.balance) : account.balanceError || "尚未确认网站账号余额"}>{account.balanceFreshness === "fresh" ? formatBalance(account.balance) : "待刷新"}</dd>{account.balanceFreshness !== "fresh" && account.balance && <small title={account.balanceError}>上次 {formatBalance(account.balance)}</small>}</div>
        <div><dt>Codex API</dt><dd>{configuredKeys.length} <small>已导入{remoteKeyCount > configuredKeys.length ? ` / 站点 ${remoteKeyCount}` : ""}</small></dd></div>
        <div className="model-count-metric"><dt>可用模型 <ChevronRight size={13} /></dt><dd>{account.selectedKeyId ? source?.models?.length || "待同步" : 0}</dd><button className="metric-open-button" aria-label={`查看 ${account.siteName || "中转站"} 的可用模型`} onClick={onModels} /></div>
        <div><dt>余额更新时间</dt><dd>{account.balanceUpdatedAt ? formatTime(account.balanceUpdatedAt) : "尚未成功读取"}</dd></div>
      </dl>

      <footer className="card-actions relay-account-actions">
        <button
          className={cx("button subtle pool-toggle", syncToApi && "active")}
          type="button"
          onClick={() => onProxy(account, source, !syncToApi)}
          disabled={busy || !source || !account.selectedKeyId}
        >
          <Server size={16} />{syncToApi ? "已加入 API" : "加入 API"}
        </button>
        <button className="button subtle" type="button" onClick={() => onRefresh(account)} disabled={busy}>
          <RefreshCw className={busy ? "spin" : ""} size={16} />刷新
        </button>
        <button className="button subtle" type="button" onClick={() => onManage(account)} disabled={busy}>
          <Pencil size={16} />编辑
        </button>
        <button className="button subtle" type="button" onClick={() => onExport(account)} disabled={busy}>
          <Download size={16} />导出
        </button>
        <IconButton label={`删除 ${account.name || account.siteName || "中转站账号"}`} className="danger" onClick={() => onDelete(account)} disabled={busy}>
          <Trash2 size={17} />
        </IconButton>
      </footer>
    </article>
  );
}

function AccountCard({
  item,
  active,
  groups,
  selectionMode,
  selected,
  onToggle,
  onSelect,
  onGroup,
  onRefresh,
  onPortal,
  onEdit,
  onExport,
  onDelete,
  onProxy,
  onLoadReset,
  onConsumeReset,
  onModels,
  onReauthenticate,
  busy,
}) {
  const isProvider = item.kind === "provider";
  const account = item.record;
  const isRelayAccountProvider = isProvider && account.sourceType === "relay_account";
  const activationRequired = Boolean(item.activationRequired);
  const isChatgpt = !isProvider && account.authMode === "chatgpt";
  const isOpenAiKey = !isProvider && account.authMode === "apikey";
  const isAgentIdentity =
    !isProvider && account.authMode === "agent_identity";
  const isPersonalAccessToken =
    !isProvider && account.authMode === "personal_access_token";
  const cardTypeClass = isProvider
    ? isRelayAccountProvider
      ? "account-type-relay"
      : "account-type-provider"
    : isChatgpt
      ? "account-type-official"
      : "account-type-credential";
  const quotaOnly = Boolean(account.quotaOnly || account.codexCompatible === false);
  const usage = account.usage || {};
  const invalid = Boolean(
    account.invalid ||
      (isProvider && account.modelDiscoveryState === "error"),
  );
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const planLabel = isOpenAiKey
    ? "API Key"
    : isPersonalAccessToken
      ? "PAT"
    : subscriptionBadge([account.planLabel, account.plan, usage.plan], false).label;
  const freeAccount = isChatgpt && isFreePlan(account.plan, planLabel, usage.plan);
  const balance = isProvider
    ? account.balance
      ? formatBalance(account.balance)
      : account.balanceError
        ? "站点未开放"
      : account.balanceEndpoint
        ? "待刷新"
        : "待识别"
    : null;
  const resetKnown =
    usage.resetCredits != null &&
    Number.isFinite(Number(usage.resetCredits?.availableCount));
  const resetCount = resetKnown
    ? Math.max(0, Number(usage.resetCredits.availableCount))
    : null;
  const resetCredits = usage.resetCredits?.credits || [];
  const nextResetExpiry =
    resetCredits
      .map((credit) => credit.expiresAt)
      .filter(Boolean)
      .sort()[0] || null;
  const subscriptionExpiry =
    usage.subscriptionExpiresAt || account.subscriptionExpiresAt || null;
  const validityExpiry = subscriptionExpiry;
  const validityKind = "订阅有效期";
  const rawValidity = remainingDuration(validityExpiry);
  const subscriptionMetadataSource =
    usage.subscriptionMetadataSource ||
    account.subscriptionMetadataSource ||
    "";
  const unverifiedExpiredSubscription = Boolean(
    subscriptionExpiry &&
      rawValidity.expired &&
      !["entitlement", "usage"].includes(subscriptionMetadataSource),
  );
  const validity = unverifiedExpiredSubscription
    ? { known: false, label: "待同步", expired: false }
    : rawValidity;
  const importedAt = account.importedAt || account.createdAt;
  const refreshDiagnostics = accountRefreshDiagnostics(account);
  const refreshErrors = refreshDiagnostics.filter(item => item.severity === "error");
  const refreshWarnings = refreshDiagnostics.filter(item => item.severity === "warning");
  const canReauthenticate = !isProvider && account.authMode === "chatgpt" && account.sourceType === "codex_auth";
  const needsReauthentication = canReauthenticate && /401|invalid.grant|refresh.token|登录.*失效|认证.*失败/i.test(JSON.stringify(account.refreshErrors || {}));
  return (
    <article
      data-card-id={item.id}
      data-card-group-id={item.groupId || account.groupId}
      className={cx(
        "account-card",
        cardTypeClass,
        !isChatgpt && "service-account-card",
        `card-tone-${item.group?.color || "slate"}`,
        active && "active",
        activationRequired && "activation-required",
        invalid && "invalid",
        selected && "selected",
      )}
    >
      <div className="account-card-head account-select">
        {selectionMode && (
          <button
            className={cx("card-checkbox", selected && "checked")}
            onClick={() => onToggle(item)}
            aria-pressed={selected}
            aria-label={
              selected ? `取消选择 ${item.name}` : `选择 ${item.name}`
            }
          >
            {selected ? <CheckSquare size={20} /> : <Square size={20} />}
          </button>
        )}
        <span
          className={cx(
            "account-avatar",
            isProvider ? "provider" : isChatgpt ? "openai" : "credential",
          )}
        >
          {isProvider ? <Server size={22} /> : <Globe2 size={22} />}
        </span>
        <span className="account-title">
          <span>
            <strong>{item.name}</strong>
            {active && (
              <em className={activationRequired ? "warning" : undefined}>
                {activationRequired ? <AlertTriangle size={12} /> : <Check size={12} />}
                {activationRequired ? "需重新应用" : "当前使用"}
              </em>
            )}
          </span>
          {isProvider ? (
            <small className="provider-address">
              <span>{account.baseUrl}</span>
              {account.portalUrl && (
                <button
                  type="button"
                  onClick={() => onPortal(item)}
                  disabled={busy}
                  aria-label={`打开 ${item.name} 官网或 Key 管理页`}
                  title="用默认浏览器打开管理站，保留浏览器登录状态"
                >
                  <ExternalLink size={13} />
                </button>
              )}
            </small>
          ) : (
            <small>
              {account.email ||
                account.name ||
                (isOpenAiKey
                  ? "OpenAI API Key"
                  : isAgentIdentity
                    ? "Codex 程序化身份"
                  : isPersonalAccessToken
                    ? "Codex Personal Access Token"
                    : "ChatGPT 账号")}
            </small>
          )}
        </span>
        <button
          className={cx("play-source", active && !activationRequired && "active")}
          onClick={() => onSelect(item)}
          disabled={busy || quotaOnly}
          aria-label={
            quotaOnly
              ? `${item.name} 仅支持额度查询`
              : active && !activationRequired
                ? "重新应用当前账号并重启 Codex"
              : activationRequired
                ? `重新应用 ${item.name} 的官方登录`
                : `切换到 ${item.name}`
          }
          title={
            quotaOnly
              ? "Web Session 不含 Codex OAuth 凭据，仅支持额度查询"
              : active && !activationRequired
                ? "重新应用当前账号并重启 Codex，清除旧进程的环境影响"
              : activationRequired
                ? "当前认证文件含有与 OAuth 凭据冲突的显式声明；点击后安全重新应用"
                : `切换到 ${item.name}`
          }
        >
          {active && !activationRequired ? (
            <Check size={20} />
          ) : activationRequired ? (
            <RefreshCw size={19} />
          ) : (
            <Play size={19} fill="currentColor" />
          )}
        </button>
      </div>
      <div className="account-tags">
        <RefinedSelect variant="group" value={item.groupId || ""} ariaLabel={`${item.name} 分组`} disabled={busy}
          onChange={groupId => onGroup(item, groupId)}
          options={groups.map(group => ({value:group.id,label:group.name,tone:group.color}))} />
        {isProvider ? (
          <span
            className="login-method-badge manual-login"
            title={
              account.keyConfigured
                ? "API Key 已在本机加密保存"
                : "尚未配置 API Key"
            }
          >
            <KeyRound size={12} />手动登录
          </span>
        ) : (
          <>
            <span>
              {isOpenAiKey
                ? "OpenAI API Key"
                : isAgentIdentity
                  ? "Agent Identity"
                  : isPersonalAccessToken
                    ? "Personal Access Token"
                    : account.sourceType === "web_session"
                      ? "Web Session"
                      : "Codex Auth"}
            </span>
            {isChatgpt ? (
              <SubscriptionBadge
                label={account.planLabel}
                rawPlan={account.plan}
                usagePlan={usage.plan}
              />
            ) : (
              <span>{planLabel}</span>
            )}
            {quotaOnly && <span className="quota-only-tag">仅额度 · 不可对话</span>}
            {account.credentialCapability === "codex_short_lived" && (
              <span className="short-lived-tag">短期 Codex · 无自动续期</span>
            )}
            {invalid && (
              <span className="danger-tag">
                {account.invalidReason || "已失效"}
              </span>
            )}
          </>
        )}
      </div>
      {activationRequired && (
        <p className="identity-reapply-note">
          <AlertTriangle size={15} />
          认证文件中的显式类型声明与实际 OAuth 凭据冲突；点击右上角按钮重新应用。桌面端账户菜单的外观不作为 API / OAuth 判断依据。
        </p>
      )}
      <div className="card-metrics account-metrics">
        <div className="model-count-metric">
          <span>可用模型 <ChevronRight size={13} /></span>
          <strong>{(item.models || account.models || []).length}</strong>
          <button className="metric-open-button" aria-label={`查看 ${item.name} 的可用模型`} onClick={onModels} />
        </div>
        <div>
          <span>
            {isProvider
              ? "剩余额度"
              : isPersonalAccessToken
                ? "凭据类型"
                : freeAccount
                  ? "账号类型"
                  : "订阅类型"}
          </span>
          <strong>{isProvider ? balance : planLabel}</strong>
        </div>
        <div>
          <span>最近刷新</span>
          <strong>
            {formatTime(
              account.lastRefreshedAt ||
                account.discoveredAt ||
                account.lastCheckedAt,
            )}
          </strong>
        </div>
      </div>
      {isProvider && !isRelayAccountProvider && (
        <section className="service-detail-section" aria-label={`${item.name} 接入概况`}>
        <header className="service-section-heading">
          <span><Route size={14} /><strong>接入能力</strong></span>
          <small>{providerIntegrationLabel(account)}</small>
        </header>
        <div className="provider-capability-grid">
          <span>
            <Route size={15} />
            <small>接口协议</small>
            <strong>
              {String(account.wireApi || "responses").includes("chat")
                ? "Chat Completions"
                : "Responses API"}
            </strong>
          </span>
          <span>
            <ShieldCheck size={15} />
            <small>凭据保护</small>
            <strong>{account.keyConfigured ? "本机加密保存" : "需要补充 Key"}</strong>
          </span>
          <span>
            <Database size={15} />
            <small>模型目录</small>
            <strong>
              {(account.models || []).length
                ? `${account.models.length} 个已同步`
                : account.modelDiscoveryState === "pending"
                  ? "等待首次同步"
                  : "跟随服务端"}
            </strong>
          </span>
          <span>
            <Globe2 size={15} />
            <small>API 主机</small>
            <strong>{endpointHost(account.resolvedBaseUrl || account.baseUrl)}</strong>
          </span>
        </div>
        {account.balance && (
          <div className="provider-usage-strip" aria-label={`${item.name} 额度详情`}>
            <span><small>识别类型</small><strong>{providerIntegrationLabel(account)}</strong></span>
            <span><small>已用额度</small><strong>{formatBalanceAmount(account.balance.used, account.balance)}</strong></span>
            <span><small>额度上限</small><strong>{account.balance.unlimited ? "不限额度" : formatBalanceAmount(account.balance.limit, account.balance)}</strong></span>
            <span><small>套餐 / 状态</small><strong>{account.balance.planName || (account.balance.mode === "unrestricted" ? "不限额" : account.balance.valid === false ? "不可用" : "可用")}</strong></span>
          </div>
        )}
        </section>
      )}
      {isChatgpt && (
        <div className="quota-stack">
          <QuotaBar label="周额度" quota={usage.weekly} unavailable={account.refreshState === "pending" || Boolean(account.refreshErrors?.usage || account.refreshErrors?.usageQuota)} />
          <QuotaEstimate estimate={account.quotaEstimate} />
        </div>
      )}
      {quotaOnly && (
        <p className="web-session-capability">
          <AlertTriangle size={15} />
          {account.codexAccessProbe?.compatible == null && !freeAccount
            ? "浏览器会话已本地转换，但 Codex 授权检测尚未完成；当前只启用额度查询。"
            : "浏览器会话可刷新额度和模型目录，但没有通过 Codex 推理授权检测。"}
        </p>
      )}
      {isProvider &&
        [account.modelDiscoveryState, account.lastCheckStatus].includes("stale") && (
          <div className="provider-model-note">
            <AlertTriangle size={14} />
            <span>
              自动模型目录不可用，已保留 {(account.models || []).length} 个已配置模型
            </span>
          </div>
        )}
      {isChatgpt && (
        <div className="account-lifecycle">
          {freeAccount ? (
            <div className="validity-banner free-account">
              <ShieldCheck size={18} />
              <span>
                <small>账号类型</small>
                <strong>Free · 无订阅到期时间</strong>
              </span>
              <time>仅按周额度状态刷新</time>
            </div>
          ) : (
            <div
              className={cx(
                "validity-banner",
                validity.expired && "expired",
                !validity.known && "unknown",
              )}
            >
              <CalendarDays size={18} />
              <span>
                <small>{validityKind}</small>
                <strong>
                  {validity.known
                    ? `有效期 ${validity.label}`
                    : "订阅状态待同步"}
                </strong>
              </span>
              <time>
                {unverifiedExpiredSubscription
                  ? `上次记录 ${formatDateTime(validityExpiry)}`
                  : validityExpiry
                    ? formatDateTime(validityExpiry)
                    : "官方暂未返回到期时间"}
              </time>
            </div>
          )}
          <div className="lifecycle-meta">
            <span className="imported-time">
              <Upload size={14} />
              <span>
                <small>导入时间</small>
                <strong>{formatDateTime(importedAt)}</strong>
              </span>
            </span>
            <button
              className={cx(
                "reset-card-counter",
                resetCount > 0 && "available",
                !resetKnown && "unknown",
              )}
              onClick={async () => {
                if (!resetKnown) return;
                if (resetCount > 0 && !usage.resetCredits?.detailsAvailable)
                  await onLoadReset(item);
                setResetOpen(true);
              }}
              disabled={!resetKnown}
              aria-label={
                resetKnown
                  ? `${item.name} 有 ${resetCount} 张重置卡`
                  : `${item.name} 重置卡数量待同步`
              }
            >
              <RotateCcw size={15} />
              <span>
                <small>官方重置卡</small>
                <strong>{resetKnown ? `${resetCount} 张` : "同步中"}</strong>
              </span>
              <em>
                {nextResetExpiry
                  ? `有效至 ${formatTime(nextResetExpiry)}`
                  : resetKnown && resetCount === 0
                    ? "当前暂无"
                    : "获取有效期"}
              </em>
            </button>
          </div>
          {account.tokenExpiresAt && (
            <button
              className="credential-detail"
              onClick={() => setDetailsOpen((value) => !value)}
              aria-expanded={detailsOpen}
            >
              <Clock3 size={14} />
              <span>登录凭据有效期</span>
              <strong>{remainingDuration(account.tokenExpiresAt).label}</strong>
              {detailsOpen ? (
                <ChevronDown size={14} />
              ) : (
                <ChevronRight size={14} />
              )}
            </button>
          )}
          {detailsOpen && (
            <div className="credential-detail-panel">
              <span>登录凭据到期时间</span>
              <strong>{formatDateTime(account.tokenExpiresAt)}</strong>
              <small>
                OAuth 凭据通常会自动刷新；这里不等同于订阅到期时间。
              </small>
              {canReauthenticate && <button type="button" className="button secondary compact" onClick={onReauthenticate} disabled={busy}><KeyRound size={15}/>重新认证</button>}
            </div>
          )}
        </div>
      )}
      {refreshErrors.map(entry => <p className="card-warning advisory" key={entry.operation}>
        <AlertTriangle size={14} /><span><strong>{entry.label}：</strong>{entry.message}</span>
      </p>)}
      {needsReauthentication && <div className="card-footnote"><button type="button" className="button secondary compact" onClick={onReauthenticate} disabled={busy}><KeyRound size={15}/>重新认证此账号</button><span>保留账号与分组</span></div>}
      {refreshWarnings.length > 0 && <details className="account-refresh-details">
        <summary><Info size={14} />{refreshWarnings.length} 项附加信息待更新<ChevronDown size={13} /></summary>
        <div>{refreshWarnings.map(entry => <p key={entry.operation}><strong>{entry.label}</strong><span>{entry.message}</span></p>)}</div>
      </details>}
      <footer className="card-actions">
        {((isChatgpt && !quotaOnly) || isProvider) && (
          <button
            className={cx(
              "button subtle pool-toggle",
              account.proxyEnabled && "active",
            )}
            onClick={() => onProxy(item, !account.proxyEnabled)}
            disabled={busy}
            aria-pressed={Boolean(account.proxyEnabled)}
            title="加入本地 OpenAI 兼容 API 号池"
          >
            <Server size={16} />
            {account.proxyEnabled ? "已加入 API" : "加入 API"}
          </button>
        )}
        {!isOpenAiKey && !isAgentIdentity && !isPersonalAccessToken && (
          <button
            className="button subtle"
            onClick={() => onRefresh(item)}
            disabled={busy}
          >
            <RefreshCw size={16} />
            刷新
          </button>
        )}
        <button
          className="button subtle"
          onClick={() => onEdit(item)}
          disabled={busy}
        >
          <Pencil size={16} />
          编辑
        </button>
        <button
          className="button subtle"
          onClick={() => onExport(item)}
          disabled={busy}
        >
          <Download size={16} />
          导出
        </button>
        <IconButton
          label={`删除 ${item.name}`}
          className="danger"
          onClick={() => onDelete(item)}
          disabled={busy}
        >
          <Trash2 size={17} />
        </IconButton>
      </footer>
      {resetOpen && (
        <ResetCreditModal
          item={item}
          busy={busy}
          onClose={() => setResetOpen(false)}
          onConsume={async () => {
            const consumed = await onConsumeReset(item);
            if (consumed) setResetOpen(false);
          }}
        />
      )}
    </article>
  );
}

function looksLikeTotpSecret(rawValue) {
  const value = String(rawValue || "").trim();
  if (!value) return false;
  if (/^(?:otpauth|otpauth-migration):\/\//i.test(value)) return true;
  if (/^https?:\/\/(?:www\.)?2fa\.show\//i.test(value)) return true;
  const compact = value.replace(/[\s-]+/g, "").replace(/=+$/, "");
  return compact.length >= 16 && compact.length <= 256 && /^[A-Z2-7]+$/i.test(compact);
}

function splitOAuthLoginBundle(rawValue) {
  const value = String(rawValue || "").trim();
  if (!value)
    return {
      account: "",
      password: "",
      secret: "",
      bundled: false,
      mailCandidate: false,
    };
  let parts = value.split(/\s*(?:-{2,}|_{2,}|[—–－]{2,})\s*/).map((part) => part.trim());
  if (parts.length < 2) {
    for (const separator of ["\t", "|", ":::"]) {
      const candidate = value.split(separator).map((part) => part.trim());
      if ([2, 3, 4].includes(candidate.length) && candidate.every(Boolean)) {
        parts = candidate;
        break;
      }
    }
  }
  if (parts.length >= 2 && parts[0] && parts.at(-1)) {
    const trailingSecret = looksLikeTotpSecret(parts.at(-1)) ? parts.at(-1) : "";
    const twoFactorBundle = Boolean(trailingSecret) && parts.length <= 3;
    return {
      account: parts[0],
      password: parts.length >= 3 ? parts[1] : "",
      // Two fields are account/2FA. Three fields are account/password/2FA
      // only when the final value is recognizably TOTP-shaped; otherwise an
      // email/password/token bundle remains available to the mailbox helper.
      secret: twoFactorBundle ? trailingSecret : "",
      bundled: true,
      mailCandidate: parts[0].includes("@") && !twoFactorBundle && parts.length >= 3,
    };
  }
  return {
    account: "",
    password: "",
    secret: value,
    bundled: false,
    mailCandidate: false,
  };
}

function OAuthTwoFactorCard({ notify, input, onInputChange }) {
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [clock, setClock] = useState(Date.now());
  const [mailAccount, setMailAccount] = useState(null);
  const [mailMessages, setMailMessages] = useState([]);
  const [mailError, setMailError] = useState("");
  const [mailBusy, setMailBusy] = useState(false);
  const [mailPaused, setMailPaused] = useState(false);
  const requestSerial = useRef(0);
  const mailRequestSerial = useRef(0);
  const mailChecks = useRef(0);
  const mailInFlight = useRef(false);
  const bundle = useMemo(() => splitOAuthLoginBundle(input), [input]);

  const generate = useCallback(
    async (quiet = false) => {
      if (!bundle.secret.trim()) return;
      const serial = ++requestSerial.current;
      if (!quiet) setBusy(true);
      try {
        const response = await api("/api/toolbox/totp/generate", {
          method: "POST",
          body: JSON.stringify({ input: bundle.secret, save: false }),
        });
        if (serial !== requestSerial.current) return;
        setResult(response.result);
        setError("");
      } catch (generationError) {
        if (serial !== requestSerial.current) return;
        setResult(null);
        setError(generationError.message);
      } finally {
        if (serial === requestSerial.current) setBusy(false);
      }
    },
    [bundle.secret],
  );

  useEffect(() => {
    requestSerial.current += 1;
    setResult(null);
    setError("");
    if (!bundle.secret.trim()) return undefined;
    const timer = window.setTimeout(() => generate(), 260);
    return () => window.clearTimeout(timer);
  }, [bundle.secret, generate]);

  useEffect(() => {
    mailRequestSerial.current += 1;
    mailChecks.current = 0;
    mailInFlight.current = false;
    setMailAccount(null);
    setMailMessages([]);
    setMailError("");
    setMailPaused(false);
    if (!bundle.mailCandidate || result || (bundle.secret && !error)) return undefined;
    const serial = mailRequestSerial.current;
    const timer = window.setTimeout(async () => {
      try {
        const response = await api("/api/toolbox/mail/preview", {
          method: "POST",
          body: JSON.stringify({ text: input }),
        });
        if (serial !== mailRequestSerial.current) return;
        const valid = response.preview?.items?.filter((item) => item.valid) || [];
        if (valid.length === 1) {
          setMailAccount(valid[0]);
          setMailError("");
        } else if (valid.length > 1) {
          setMailError("登录助手一次只读取一个邮箱账号");
        }
      } catch (previewError) {
        if (serial === mailRequestSerial.current)
          setMailError(previewError.message || "无法识别邮箱登录信息");
      }
    }, 420);
    return () => window.clearTimeout(timer);
  }, [input, bundle.mailCandidate, bundle.secret, result, error]);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const remaining = result?.validUntil
    ? Math.max(0, Math.ceil((new Date(result.validUntil).getTime() - clock) / 1000))
    : 0;

  useEffect(() => {
    if (!result?.validUntil || remaining > 0 || busy) return;
    generate(true);
  }, [remaining, result?.validUntil, busy, generate]);

  const newestMailCode = useMemo(
    () =>
      mailMessages
        .flatMap((message) => (Array.isArray(message.codes) ? message.codes : []))
        .find(Boolean) || "",
    [mailMessages],
  );

  const fetchMailbox = useCallback(
    async (manual = false) => {
      if (!mailAccount || mailInFlight.current) return false;
      if (!manual && mailChecks.current >= OAUTH_MAIL_MAX_AUTOMATIC_CHECKS) {
        setMailPaused(true);
        return false;
      }
      mailInFlight.current = true;
      mailChecks.current += 1;
      setMailBusy(true);
      try {
        const response = await api("/api/toolbox/mail/temporary/messages", {
          method: "POST",
          body: JSON.stringify({ text: input, limit: 10, unreadOnly: false }),
        });
        const messages = Array.isArray(response.messages) ? response.messages : [];
        setMailMessages(messages);
        setMailError("");
        setMailPaused(false);
        return messages.some(
          (message) => Array.isArray(message.codes) && message.codes.length,
        );
      } catch (mailFetchError) {
        setMailError(mailFetchError.message || "暂时无法读取邮箱");
        return false;
      } finally {
        mailInFlight.current = false;
        setMailBusy(false);
      }
    },
    [input, mailAccount],
  );

  useEffect(() => {
    if (!mailAccount || result || newestMailCode) return undefined;
    let stopped = false;
    let timer;
    const check = async () => {
      const found = await fetchMailbox(false);
      if (stopped || found) return;
      if (mailChecks.current >= OAUTH_MAIL_MAX_AUTOMATIC_CHECKS) {
        setMailPaused(true);
        return;
      }
      timer = window.setTimeout(check, OAUTH_MAIL_POLL_MS);
    };
    check();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [mailAccount, result, newestMailCode, fetchMailbox]);

  const copyPart = async (value, label) => {
    try {
      await copyText(value);
      notify(`${label}已复制`);
    } catch (copyError) {
      notify(copyError.message, "error");
    }
  };

  return (
    <aside className="oauth-2fa-card" aria-label="OAuth 登录辅助验证码">
      <div className="oauth-2fa-heading">
        <div>
          <span>LOGIN HELPER</span>
          <h3>登录与 2FA 助手</h3>
          <p>登录成功或取消授权后清空；误关窗口不会丢失。</p>
        </div>
        <ShieldCheck size={21} />
      </div>
      <label className="field">
        <span>2FA 密钥或账号组合</span>
        <textarea
          value={input}
          onChange={(event) => onInputChange(event.target.value)}
          placeholder={
            "JBSWY3DPEHPK3PXP\n账号----密码----2FA\n邮箱----密码----access_token\n邮箱----密码----refresh_token----client_id"
          }
          spellCheck={false}
          autoComplete="off"
          rows={4}
        />
      </label>
      <small className="oauth-2fa-formats">
        支持 Base32、otpauth://、2fa.show、Authenticator 迁移，以及常见邮箱密码 / OAuth Token 组合。
      </small>
      {bundle.bundled && (
        <div className="oauth-login-parts">
          <button type="button" onClick={() => copyPart(bundle.account, "账号")} title={bundle.account}>
            <span>账号</span>
            <strong>{bundle.account}</strong>
            <Copy size={14} />
          </button>
          {bundle.password && (
            <button type="button" onClick={() => copyPart(bundle.password, "密码")} title="点击复制密码">
              <span>密码</span>
              <strong>{"•".repeat(Math.min(12, Math.max(6, bundle.password.length)))}</strong>
              <Copy size={14} />
            </button>
          )}
        </div>
      )}
      <div
        className={cx(
          "oauth-live-code",
          !mailAccount && error && "error",
          !result && !mailAccount && !error && "empty",
        )}
        aria-live="polite"
      >
        <div>
          <span>{mailAccount ? `邮箱验证码 · ${mailAccount.email}` : "实时一次性验证码"}</span>
          {busy && !result ? (
            <strong className="oauth-code-loading"><Loader2 className="spin" size={20} /> 正在识别</strong>
          ) : result ? (
            <button type="button" onClick={() => copyPart(result.code, "验证码")}>{result.code}</button>
          ) : mailBusy && !mailMessages.length ? (
            <strong className="oauth-code-loading"><Loader2 className="spin" size={20} /> 正在检查邮箱</strong>
          ) : newestMailCode ? (
            <button type="button" onClick={() => copyPart(newestMailCode, "邮箱验证码")}>{newestMailCode}</button>
          ) : mailAccount ? (
            <strong>
              {mailError || (mailPaused ? "自动检查已暂停，可手动再次检查" : "等待新邮件，20 秒后再次检查")}
            </strong>
          ) : (
            <strong>{error || "输入后自动生成"}</strong>
          )}
        </div>
        {result && (
          <div className="oauth-code-expiry">
            <span style={{ "--otp-progress": `${Math.max(0, Math.min(100, (remaining / (result.period || 30)) * 100))}%` }} />
            <small>{remaining} 秒后自动刷新 · {result.algorithm || "SHA1"}</small>
          </div>
        )}
        {mailAccount && !newestMailCode && (
          <button
            type="button"
            className="oauth-mail-check"
            onClick={() => fetchMailbox(true)}
            disabled={mailBusy}
          >
            <RefreshCw className={mailBusy ? "spin" : ""} size={14} />
            立即检查邮箱
          </button>
        )}
      </div>
      <p className="oauth-2fa-privacy">
        <ShieldCheck size={13} />
        不保存账号、密码、Token 或 2FA 密钥。邮箱仅在当前授权界面内每 20 秒检查一次，最多 12 次，识别到验证码后立即停止。
      </p>
    </aside>
  );
}

function RelayLoginPanel({
  groupId,
  syncToApi,
  notify,
  onDone,
  initialPortalUrl = "",
  relayAccountId = "",
  autoStart = false,
  onOriginReconnect,
}) {
  const [portalUrl, setPortalUrl] = useState(initialPortalUrl);
  const [status, setStatus] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [endpointId, setEndpointId] = useState("");
  const [relayGroupId, setRelayGroupId] = useState("");
  const [keyName, setKeyName] = useState("Agent Manager");
  const [busyAction, setBusyAction] = useState("");
  const selectedPreview = useRef("");
  const startPromise = useRef(null);
  const mounted = useRef(false);
  const statusRef = useRef(status);
  const flowComplete = useRef(false);
  const completedSession = useRef("");
  const onDoneRef = useRef(onDone);
  statusRef.current = status;
  onDoneRef.current = onDone;
  const reauth = Boolean(relayAccountId);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      const owned = statusRef.current;
      window.setTimeout(() => {
        if (!mounted.current && !flowComplete.current && owned?.sessionId && !["reauthenticated", "imported", "cancelled", "closed", "idle"].includes(owned.status)) {
          api("/api/relay-login/cancel", { method: "POST", body: JSON.stringify({ sessionId: owned.sessionId }) }).catch(() => {});
        }
      }, 0);
    };
  }, []);

  const refreshStatus = useCallback(async () => {
    const result = await api("/api/relay-login/status");
    if (relayAccountId && initialPortalUrl && result.status?.portalUrl) {
      try {
        if (new URL(initialPortalUrl).origin !== new URL(result.status.portalUrl).origin) {
          setStatus(null);
          return null;
        }
      } catch {
        setStatus(null);
        return null;
      }
    }
    if (relayAccountId && result.status?.accountId && result.status.accountId !== relayAccountId) return null;
    setStatus(result.status);
    if (result.status?.portalUrl)
      setPortalUrl((current) => current || result.status.portalUrl);
    return result.status;
  }, [initialPortalUrl, relayAccountId]);

  useEffect(() => {
    let active = true;
    if (autoStart && initialPortalUrl.trim()) {
      setBusyAction("start");
      startPromise.current ||= api("/api/relay-login/start", {
        method: "POST", body: JSON.stringify({ url: initialPortalUrl.trim(), accountId: relayAccountId || undefined }), timeoutMs: 45_000,
      });
      startPromise.current.then(result => {
        if (!active) {
          if (!mounted.current && result.status?.sessionId) api("/api/relay-login/cancel", { method: "POST", body: JSON.stringify({ sessionId: result.status.sessionId }) }).catch(() => {});
          return;
        }
        selectedPreview.current = "";
        setSelected(new Set());
        setStatus(result.status);
      }).catch(error => { if (active) notify(error.message, "error"); })
        .finally(() => { if (active) setBusyAction(""); });
    } else {
      refreshStatus().catch(() => {
        if (active) setStatus(null);
      });
    }
    return () => {
      active = false;
    };
  }, [autoStart, initialPortalUrl, refreshStatus]);

  useEffect(() => {
    if (!status?.sessionId || !["waiting_login", "checking_login", "scanning", "reauth_syncing"].includes(status.status)) return undefined;
    let active = true;
    let timer;
    const check = async () => {
      try {
        const response = await api("/api/relay-login/check", { method: "POST", body: JSON.stringify({ sessionId: status.sessionId }), timeoutMs: 90_000 });
        if (!active) return;
        setStatus(response.status);
        if (["waiting_login", "checking_login", "scanning", "reauth_syncing"].includes(response.status?.status)) timer = window.setTimeout(check, Math.max(1500, Math.min(10000, response.status?.autoAuth?.nextCheckInMs || 2500)));
      } catch (error) {
        if (!active) return;
        setStatus(current => ({ ...current, error: error.message }));
        timer = window.setTimeout(check, 6000);
      }
    };
    timer = window.setTimeout(check, Math.max(500, status.autoAuth?.nextCheckInMs || 800));
    return () => { active = false; window.clearTimeout(timer); };
  }, [status?.sessionId, status?.status]);

  useEffect(() => {
    if (!reauth || status?.status !== "reauthenticated" || completedSession.current === status.sessionId) return;
    completedSession.current = status.sessionId;
    flowComplete.current = true;
    const warnings = status.completion?.warnings || [];
    notify(warnings.length ? `登录已恢复，原选择已保留；${warnings[0]}` : "登录已恢复，原来的 Key 和分组已保留，账号已继续刷新", warnings.length ? "warning" : "success");
    Promise.resolve(onDoneRef.current()).catch(error => notify(error.message, "error"));
  }, [reauth, status?.status, status?.sessionId]);

  useEffect(() => {
    const preview = status?.preview;
    const signature = status?.sessionId || "";
    if (reauth || !preview || !signature || selectedPreview.current === signature) return;
    selectedPreview.current = signature;
    const compatibleKeys = (preview.keys || []).filter(isCodexRelayRecord);
    setSelected(new Set(compatibleKeys.length === 1 ? [String(compatibleKeys[0].id)] : []));
    const compatibleEndpoints = (preview.apiEndpoints || []).filter(isCodexRelayEndpoint);
    const compatibleGroups = (preview.groups || []).filter(isCodexRelayRecord);
    const preferredEndpoint = compatibleEndpoints.find(
      (item) => String(item.id) === String(preview.defaultEndpointId),
    ) || compatibleEndpoints.find((item) => item.isDefault) || compatibleEndpoints[0];
    setEndpointId(preferredEndpoint?.id || "default");
    setRelayGroupId(
      String(
        compatibleGroups.find((item) => item.active)?.id
        || compatibleGroups[0]?.id
        || "",
      ),
    );
  }, [status?.sessionId, status?.preview]);

  const run = async (action, task) => {
    setBusyAction(action);
    try {
      return await task();
    } catch (error) {
      notify(error.message, "error");
      throw error;
    } finally {
      setBusyAction("");
    }
  };

  const startLogin = async (override) => {
    const destination = typeof override === "string" ? override : portalUrl.trim();
    if (!destination) return;
    try {
      await run("start", async () => {
        const result = await api("/api/relay-login/start", {
          method: "POST",
          body: JSON.stringify({ url: destination, accountId: relayAccountId || undefined }),
          timeoutMs: 45_000,
        });
        selectedPreview.current = "";
        setSelected(new Set());
        setStatus(result.status);
        flowComplete.current = false;
      });
    } catch {
      /* run already reported the actionable error */
    }
  };

  const scanLogin = async () => {
    if (!status?.sessionId) return;
    try {
      await run("scan", async () => {
        setStatus((current) => ({
          ...current,
          status: "scanning",
          message: "正在识别账户、余额、模型与 API Key…",
          error: null,
        }));
        const result = await api("/api/relay-login/check", {
          method: "POST",
          body: JSON.stringify({ sessionId: status.sessionId, force: true }),
          timeoutMs: 90_000,
        });
        setStatus(result.status);
        notify(result.status.message || "中转站内容已识别");
      });
    } catch {
      refreshStatus().catch(() => {});
    }
  };

  const importSelected = async () => {
    if (!status?.sessionId) return;
    try {
      await run("import", async () => {
        const result = await api("/api/relay-login/import", {
          method: "POST",
          body: JSON.stringify({
            sessionId: status.sessionId,
            keyIds: [...selected],
            endpointId,
            groupId,
            proxyEnabled: syncToApi,
            relayAccountId,
          }),
          timeoutMs: 180_000,
        });
        const failed = result.result?.failed?.length || 0;
        const warnings = result.result?.warnings || [];
        const imported = result.result?.count ?? selected.size;
        notify(
          warnings.length
            ? `已导入 ${imported} 个 Key；${warnings[0]}`
            : failed
            ? `已导入 ${imported} 个选中的 Codex API Key，${failed} 个未能读取`
            : `已导入选中的 ${imported} 个 Codex API Key，网页登录凭据已加密保存，登录窗口已自动关闭${syncToApi ? "；当前 Key 已加入本地 API 号池" : ""}`,
          failed || warnings.length ? "warning" : undefined,
        );
        flowComplete.current = true;
        await onDone();
      });
    } catch {
      /* run already reported the actionable error */
    }
  };

  const createKey = async () => {
    if (!status?.sessionId || !keyName.trim()) return;
    try {
      await run("create", async () => {
        const result = await api("/api/relay-login/create", {
          method: "POST",
          body: JSON.stringify({
            sessionId: status.sessionId,
            name: keyName.trim(),
            endpointId,
            relayGroupId,
            groupId,
            proxyEnabled: syncToApi,
            relayAccountId,
          }),
          timeoutMs: 180_000,
        });
        notify(`已创建“${keyName.trim()}”；请在下方勾选后再导入`);
        setStatus(result.result?.status || await refreshStatus());
      });
    } catch {
      /* run already reported the actionable error */
    }
  };

  const cancelLogin = async () => {
    try {
      await run("cancel", async () => {
        const result = await api("/api/relay-login/cancel", {
          method: "POST",
          body: JSON.stringify({ sessionId: status?.sessionId || "" }),
        });
        selectedPreview.current = "";
        setSelected(new Set());
        setStatus(result.status);
      });
    } catch {
      /* run already reported the actionable error */
    }
  };

  const preview = reauth ? null : status?.preview;
  const endpoints = preview?.apiEndpoints?.filter(isCodexRelayEndpoint)?.length
    ? preview.apiEndpoints.filter(isCodexRelayEndpoint)
    : preview
      ? [{
          id: "default",
          name: "默认 API",
          baseUrl: preview.baseUrl,
          description: "自动识别",
          aliases: [],
          isDefault: true,
        }]
      : [];
  const selectedEndpoint = endpoints.find((item) => item.id === endpointId) || endpoints[0];
  const relayGroups = (preview?.groups || []).filter(isCodexRelayRecord);
  const relayKeys = (preview?.keys || []).filter(isCodexRelayRecord);
  const codexModels = (preview?.models || []).filter((model) =>
    isCodexRelayRecord({ models: [model] }),
  );
  const isBusy = Boolean(busyAction);
  const hasSession = Boolean(status?.sessionId);
  const windowAvailable = ["waiting_login", "checking_login", "ready", "scanning", "reauth_syncing", "identity_mismatch", "reauth_error"].includes(status?.status);
  const readableBalance = (value, currency) => {
    if (typeof value !== "number") return "站点未提供";
    return `${currency || "USD"} ${new Intl.NumberFormat("zh-CN", {
      maximumFractionDigits: 4,
    }).format(value)}`;
  };
  const readableCost = (value) => (
    typeof value === "number"
      ? `$${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 }).format(value)}`
      : "—"
  );
  const readableRate = (value) => (
    typeof value === "number"
      ? `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 }).format(value)}×`
      : "倍率未提供"
  );
  const readableExpiry = (value) => {
    if (!value || Number(value) < 0) return "永久有效";
    const numeric = Number(value);
    const date = Number.isFinite(numeric)
      ? new Date(numeric < 10_000_000_000 ? numeric * 1000 : numeric)
      : new Date(value);
    return Number.isNaN(date.getTime()) ? "站点未提供" : formatDateTime(date.toISOString());
  };

  return (
    <section className="relay-login-panel" aria-label="中转站登录与识别">
      <div className="relay-login-intro">
        <span className="relay-login-icon"><Globe2 size={22} /></span>
        <div>
          <strong>{reauth ? "在打开的窗口完成登录即可" : "添加一个中转站"}</strong>
          <small>{reauth ? "登录后会自动刷新当前账号，继续使用原来的 Key 与分组。" : "填写网址后登录，软件会自动读取可用 Key。"}</small>
        </div>
      </div>
      <RelayOriginNotice details={status?.originMismatch} disabled={isBusy} onReconnect={url => {
        if (onOriginReconnect) onOriginReconnect(url);
        else if (!reauth) { setPortalUrl(url); startLogin(url); }
      }} />
      {(!reauth || !hasSession) && <div className="relay-login-url-row">
        <label className="field">
          <span>中转站网址</span>
          <input
            type="url"
            inputMode="url"
            value={portalUrl}
            onChange={(event) => setPortalUrl(event.target.value)}
            placeholder="https://example.com"
            disabled={isBusy}
          />
        </label>
        <button
          type="button"
          className="button primary"
          onClick={startLogin}
          disabled={isBusy || !portalUrl.trim()}
        >
          {busyAction === "start" ? <Loader2 className="spin" size={17} /> : <ExternalLink size={17} />}
          {hasSession && !windowAvailable ? "重新打开登录窗" : "打开登录窗"}
        </button>
      </div>}
      <p className="relay-login-privacy">
        <ShieldCheck size={15} />
        登录状态会在本机加密保存，用于后续刷新。账号导出包包含续期凭据。
      </p>

      {hasSession && (
        <div className={cx("relay-login-status", status?.error && "warning", status?.status === "ready" && "ready")}>
          <span>
            {["checking_login", "scanning", "reauth_syncing"].includes(status?.status) ? <Loader2 className="spin" size={18} /> : status?.error ? <AlertTriangle size={18} /> : <Globe2 size={18} />}
          </span>
          <div>
            <strong>{status?.message || "中转站登录会话已建立"}</strong>
            <small>{status?.error || status?.currentUrl || status?.portalUrl}</small>
          </div>
          <div className="relay-login-status-actions">
            {(status?.error || ["reauth_timeout", "reauth_error", "identity_mismatch"].includes(status?.status)) && <button type="button" className="button secondary" onClick={scanLogin} disabled={isBusy || !hasSession}>
              {busyAction === "scan" || status?.status === "scanning" ? <Loader2 className="spin" size={16} /> : <Search size={16} />}
              重新检测登录
            </button>}
            <button type="button" className="button subtle" onClick={cancelLogin} disabled={isBusy}>
              <X size={16} />关闭会话
            </button>
          </div>
        </div>
      )}

      {preview && (
        <div className="relay-preview">
          <header className="relay-preview-heading">
            <div>
              <span className="eyebrow">{preview.adapterLabel}</span>
              <h3>{preview.siteName}</h3>
              <p>{preview.user?.name || preview.user?.email || "已登录用户"} · {preview.user?.group || "默认分组"}</p>
            </div>
            <span className="status-pill healthy"><Check size={14} /> 已识别</span>
          </header>
          <div className="relay-preview-facts">
            <article>
              <Gauge size={17} />
              <span><small>剩余额度</small><strong>{readableBalance(preview.balance?.remaining, preview.balance?.currency)}</strong></span>
            </article>
            <article>
              <BarChart3 size={17} />
              <span><small>累计使用</small><strong>{readableBalance(preview.balance?.used, preview.balance?.currency)}</strong></span>
            </article>
            <article>
              <Database size={17} />
              <span><small>Codex 兼容模型</small><strong>{codexModels.length ? `${codexModels.length} 个` : "导入后自动同步"}</strong></span>
            </article>
            <article className="wide">
              <Server size={17} />
              <span><small>当前导入使用的 Base URL</small><strong title={selectedEndpoint?.baseUrl}>{selectedEndpoint?.baseUrl || preview.baseUrl}</strong></span>
            </article>
          </div>

          <div className="relay-endpoint-section">
            <div className="relay-section-heading">
              <span><Server size={16} /></span>
              <div>
                <strong>API 端点</strong>
                <small>站点公布的端点已全部读取；选择后，所导入的 Key 会使用该 Base URL。</small>
              </div>
            </div>
            <div className="relay-endpoint-grid">
              {endpoints.map((item) => {
                const checked = (selectedEndpoint?.id || "default") === item.id;
                return (
                  <label key={item.id} className={cx("relay-endpoint-card", checked && "selected")}>
                    <input
                      type="radio"
                      name="relay-endpoint"
                      value={item.id}
                      checked={checked}
                      disabled={isBusy}
                      onChange={() => setEndpointId(item.id)}
                    />
                    <span>
                      <strong>{item.name}{item.isDefault ? " · 默认" : ""}</strong>
                      <code>{item.baseUrl}</code>
                      <small>
                        {[
                          ...(item.aliases || []),
                          ...(item.descriptions?.length ? item.descriptions : [item.description]),
                        ].filter(Boolean).join(" · ") || "站点 API 端点"}
                      </small>
                    </span>
                    {checked && <Check size={16} />}
                  </label>
                );
              })}
            </div>
          </div>

          {relayGroups.length > 0 && (
            <div className="relay-group-section">
              <div className="relay-section-heading">
                <span><Layers3 size={16} /></span>
                <div>
                  <strong>中转站分组</strong>
                  <small>仅显示可用于 Codex / OpenAI 的 {relayGroups.length} 个分组；Claude 与生图分组已隐藏。</small>
                </div>
              </div>
              <div className="relay-group-list">
                {relayGroups.map((item) => {
                  const kind = item.platformKind || relayPlatformKind(item.platform, item.name);
                  return (
                  <article key={item.id} className={cx(!item.active && "inactive", `platform-${kind}`)}>
                    <span>
                      <strong>{item.name}</strong>
                      <small>Codex / OpenAI 兼容</small>
                    </span>
                    <em>{relayRateDetail(item)}</em>
                  </article>
                  );
                })}
              </div>
            </div>
          )}

          <div className="relay-key-toolbar">
            <div>
              <strong>选择要导入的 Codex API Key</strong>
              <small>共 {relayKeys.length} 个可见项；只会保存你勾选的项目，未选项不会写入本机</small>
            </div>
            <span>
              <button type="button" onClick={() => setSelected(new Set(relayKeys.filter((item) => item.canImport).map((item) => item.id)))} disabled={isBusy}>全选可导入</button>
              <button type="button" onClick={() => setSelected(new Set())} disabled={isBusy}>清空</button>
            </span>
          </div>
          <div className="relay-key-list">
            {relayKeys.length ? relayKeys.map((item) => {
              const checked = selected.has(item.id);
              return (
                <label key={item.id} className={cx("relay-key-row platform-codex", checked && "selected", !item.active && "inactive")}>
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={isBusy || !item.canImport}
                    onChange={() => setSelected((current) => {
                      const next = new Set(current);
                      if (next.has(item.id)) next.delete(item.id); else next.add(item.id);
                      return next;
                    })}
                  />
                  <span className="relay-key-main">
                    <strong>{item.name}</strong>
                    <small>
                      {item.maskedKey} · {item.group || "默认分组"}
                      {typeof item.groupRateMultiplier === "number" ? ` · 计费倍率 ${readableRate(item.groupRateMultiplier)}` : ""}
                      {" · Codex / OpenAI"}
                    </small>
                  </span>
                  <span className="relay-key-meta">
                    <small>{item.active ? "启用" : "停用"}</small>
                    <strong>
                      {typeof item.todayUsed === "number" || typeof item.totalUsed === "number"
                        ? `今日 ${readableCost(item.todayUsed)} · ${typeof item.thirtyDayUsed === "number" ? "近30天" : "累计"} ${readableCost(item.thirtyDayUsed ?? item.totalUsed)}`
                        : item.unlimited
                          ? "不限额度"
                          : typeof item.quota === "number"
                            ? `已用 ${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 }).format(item.used || 0)} / ${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 }).format(item.quota)}`
                            : "用量未提供"}
                    </strong>
                    <em>{readableExpiry(item.expiresAt)}</em>
                  </span>
                </label>
              );
            }) : (
              <div className="relay-key-empty"><KeyRound size={20} /><span><strong>没有可用于 Codex 的 API Key</strong><small>Claude、生图与其他不兼容项目不会显示；可在上方创建 Codex 专用 Key。</small></span></div>
            )}
          </div>
          {preview.supportsCreate && (
            <div className="relay-create-key">
              <div>
                <strong>继续创建 Codex 专用 Key</strong>
                <small>新 Key 会出现在上方列表；不会自动勾选或导入，创建后此入口会继续保留。</small>
              </div>
              <label className="field">
                <span>Key 名称</span>
                <input value={keyName} onChange={(event) => setKeyName(event.target.value)} maxLength={80} disabled={isBusy} />
              </label>
              {preview.adapter === "sub2api" && (
                <label className="field">
                  <span>Codex 分组</span>
                  <select value={relayGroupId} onChange={(event) => setRelayGroupId(event.target.value)} disabled={isBusy}>
                    {relayGroups.filter((item) => item.active).map((item) => (
                      <option key={item.id} value={String(item.id)}>
                        {item.name} · {readableRate(item.rateMultiplier)}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <button
                type="button"
                className="button secondary"
                onClick={createKey}
                disabled={isBusy || !keyName.trim() || (preview.adapter === "sub2api" && !relayGroupId)}
              >
                {busyAction === "create" ? <Loader2 className="spin" size={16} /> : <Plus size={16} />}
                创建 Key
              </button>
            </div>
          )}
          <button type="button" className="button primary full" onClick={importSelected} disabled={isBusy || selected.size === 0}>
            {busyAction === "import" ? <Loader2 className="spin" size={17} /> : <Download size={17} />}
            导入选中的 {selected.size} 个 API Key
          </button>
        </div>
      )}
    </section>
  );
}

function RelayReloginModal({ account, groupId, syncToApi, notify, onDone, onClose, browserRefresh = false, originMismatch }) {
  const [reconnectUrl, setReconnectUrl] = useState("");
  return (
    <Modal
      title={`${browserRefresh ? "通过网页刷新" : "重新登录"} ${account.name || account.siteName || "中转站账号"}`}
      description={originMismatch || reconnectUrl ? "按当前网站地址重新登录并确认导入，原卡片会保留。" : browserRefresh ? "网站要求在浏览器内验证。完成验证后会读取同一账号并同步余额。" : "完成网页登录后自动继续刷新，无需重新选择或导入 API Key"}
      onClose={onClose}
      wide
    >
      <div className="modal-body">
        {originMismatch && !reconnectUrl && <RelayOriginNotice details={originMismatch} onReconnect={setReconnectUrl} />}
        {(!originMismatch || reconnectUrl) && <RelayLoginPanel
          key={reconnectUrl || account.id}
          onOriginReconnect={setReconnectUrl}
          groupId={groupId}
          syncToApi={syncToApi}
          notify={notify}
          onDone={async () => { await onDone(); onClose(); }}
          initialPortalUrl={reconnectUrl || account.portalUrl || account.origin || ""}
          relayAccountId={reconnectUrl ? "" : account.id}
          autoStart
        />}
      </div>
    </Modal>
  );
}

function RelayAccountManageModal({ account, notify, onDone, onClose, onRelogin }) {
  const [busyKey, setBusyKey] = useState("");
  const [busyAction, setBusyAction] = useState("");
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [deleteScope, setDeleteScope] = useState("local");
  const [deleteNotice, setDeleteNotice] = useState("");
  const [portalDraft, setPortalDraft] = useState(account.portalUrl || account.origin || "");
  const [keyName, setKeyName] = useState("Agent Manager");
  const keys = (account.keys || []).filter(
    (item) => item.secretConfigured && isCodexRelayRecord(item),
  );
  const groups = (account.groups || []).filter(
    (item) => item.active !== false && isCodexRelayRecord(item),
  );
  const [relayGroupId, setRelayGroupId] = useState(
    () => String(groups[0]?.id || ""),
  );

  useEffect(() => {
    setPortalDraft(account.portalUrl || account.origin || "");
  }, [account.portalUrl, account.origin]);

  useEffect(() => {
    if (!groups.some((item) => String(item.id) === String(relayGroupId))) {
      setRelayGroupId(String(groups[0]?.id || ""));
    }
  }, [groups, relayGroupId]);

  const changeKeyGroup = async (key, groupId) => {
    setBusyKey(String(key.id));
    try {
      const result = await api(
        `/api/relay-accounts/${encodeURIComponent(account.id)}/keys/${encodeURIComponent(key.id)}/group`,
        { method: "POST", body: JSON.stringify({ groupId }) },
      );
      await onDone();
      notify(
        `${key.name} 已在中转站网站切换到 ${result.result?.account?.groups?.find((item) => String(item.id) === String(groupId))?.name || "新分组"}${result.result?.requiresReapply ? "；当前 Codex 需重新应用" : "；无需重启 Codex"}`,
        result.result?.requiresReapply ? "warning" : "success",
      );
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusyKey("");
    }
  };

  const deleteKey = async () => {
    const key = deleteTarget;
    if (!key || busyKey) return;
    setBusyKey(String(key.id));
    setDeleteNotice("");
    try {
      const response = await api(
        `/api/relay-accounts/${encodeURIComponent(account.id)}/keys/${encodeURIComponent(key.id)}`,
        { method: "DELETE", body: JSON.stringify({ scope: deleteScope }), timeoutMs: 120_000 },
      );
      const result = response.result || {};
      const reloadWarning = await onDone().then(() => "", error => `删除结果已确认，但列表读取失败：${error.message}`);
      if (result.requiresLogin) {
        notify("需要重新登录网站；登录完成后请再次确认删除范围", "warning");
        onRelogin(result.account || account); onClose(); return;
      }
      if (result.localRemoved === false) {
        setDeleteNotice([result.warnings?.[0] || (result.remoteDeleted ? "网站上的 Key 已删除，本机清理尚未完成。可选择仅删除本机重试。" : "删除未完成，请稍后重试。"),reloadWarning].filter(Boolean).join("；"));
        if (result.remoteDeleted) setDeleteScope("local");
        return;
      }
      const notice = result.warnings?.[0] || reloadWarning || (result.requiresReapply ? "当前连接已变化，请重新应用 Codex" : "");
      notify(`${key.name} 已从${deleteScope === "website" ? "网站和本机" : "本机"}移除${notice ? `；${notice}` : ""}`, notice ? "warning" : "success");
      setDeleteTarget(null);
    } catch (error) {
      setDeleteNotice(error.message);
    } finally {
      setBusyKey("");
    }
  };

  const savePortal = async () => {
    setBusyAction("portal");
    try {
      await api(`/api/relay-accounts/${encodeURIComponent(account.id)}/metadata`, {
        method: "POST",
        body: JSON.stringify({ portalUrl: portalDraft.trim() }),
      });
      await onDone();
      notify("中转站网站地址已保存", "success");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusyAction("");
    }
  };

  const openPortal = async () => {
    setBusyAction("open");
    try {
      await api(`/api/providers/${encodeURIComponent(account.providerId)}/portal`, {
        method: "POST",
        body: "{}",
      });
      notify(`已在默认浏览器打开 ${account.siteName || "中转站"}`);
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusyAction("");
    }
  };

  const createKey = async () => {
    setBusyAction("create");
    try {
      const result = await api(`/api/relay-accounts/${encodeURIComponent(account.id)}/keys`, {
        method: "POST",
        body: JSON.stringify({
          name: keyName.trim(),
          relayGroupId: account.adapter === "sub2api" ? relayGroupId : undefined,
        }),
        timeoutMs: 120_000,
      });
      await onDone();
      notify(
        `${result.result?.created?.name || keyName.trim()} 已在中转站创建并加密导入`,
        "success",
      );
      setKeyName("Agent Manager");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusyAction("");
    }
  };

  return (
    <Modal
      title={`管理 ${account.name || account.siteName || "中转站账号"}`}
      description="查看网站地址，管理已导入的 Codex API Key、倍率与分组"
      onClose={onClose}
      wide
      className="relay-account-manage-modal"
    >
      <div className="modal-body relay-account-manager">
        <div className="relay-manager-summary">
          <span><small>账号余额</small><strong>{account.balanceFreshness === "fresh" ? formatBalance(account.balance) : "待刷新"}</strong>{account.balanceFreshness !== "fresh" && account.balance && <small>上次 {formatBalance(account.balance)}</small>}</span>
          <span><small>已导入 API</small><strong>{keys.length} 个</strong></span>
          <span><small>Codex 分组</small><strong>{groups.length} 个</strong></span>
          <span><small>最近同步</small><strong>{formatTime(account.updatedAt)}</strong></span>
        </div>
        <section className="relay-manager-portal-panel" aria-label="中转站网站地址">
          <span className="relay-manager-portal-copy">
            <Globe2 size={18} />
            <span>
              <strong>中转站网站</strong>
              <small>地址常驻显示；可修改同一站点下的登录页或控制台路径。</small>
            </span>
          </span>
          <label className="field relay-manager-portal-field">
            <span className="visually-hidden">中转站网站地址</span>
            <input
              type="url"
              value={portalDraft}
              onChange={(event) => setPortalDraft(event.target.value)}
              placeholder="https://example.com/dashboard"
              disabled={Boolean(busyAction)}
            />
          </label>
          <button
            type="button"
            className="button secondary"
            onClick={savePortal}
            disabled={Boolean(busyAction) || !portalDraft.trim() || portalDraft.trim() === (account.portalUrl || account.origin || "")}
          >
            {busyAction === "portal" ? <Loader2 className="spin" size={16} /> : <Save size={16} />}
            保存
          </button>
          <button
            type="button"
            className="button secondary"
            onClick={openPortal}
            disabled={Boolean(busyAction) || !account.providerId}
          >
            {busyAction === "open" ? <Loader2 className="spin" size={16} /> : <ExternalLink size={16} />}
            查看网站
          </button>
        </section>
        <div className="relay-manager-heading">
          <span>
            <strong>已导入的 Codex API Key</strong>
            <small>每个 API 都可独立选择分组；倍率来自最近一次站点同步。</small>
          </span>
          <button className="button secondary compact" disabled={Boolean(busyAction || busyKey)} onClick={async () => {
            setBusyAction("sync");
            try {
              const response = await api(`/api/relay-accounts/${encodeURIComponent(account.id)}/refresh`, { method: "POST", body: JSON.stringify({ full: true }) });
              await onDone();
              if (response.result?.requiresLogin) { onRelogin(account); onClose(); return; }
              const warning = response.result?.warnings?.[0];
              notify(warning || "Key 与分组已同步", warning ? "warning" : "success");
            } catch (error) { notify(error.message, "error"); } finally { setBusyAction(""); }
          }}>{busyAction === "sync" ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}刷新</button>
        </div>
        <div className="relay-manager-key-list">
          {keys.map((key) => (
            <article key={key.id} className={cx("relay-manager-key", String(key.id) === String(account.selectedKeyId) && "selected")}>
              <span className="relay-manager-key-icon"><Bot size={17} /></span>
              <span className="relay-manager-key-copy">
                <strong>{key.name}{String(key.id) === String(account.selectedKeyId) ? " · 当前" : ""}</strong>
                <small>{key.maskedKey} · Codex / OpenAI · 计费倍率 {formatRelayRate(key.groupRateMultiplier)}</small>
              </span>
              <label className="relay-manager-group-select">
                <span>对应分组</span>
                <select
                  value={key.groupId || ""}
                  onChange={(event) => changeKeyGroup(key, event.target.value)}
                  disabled={Boolean(busyKey) || !groups.length}
                >
                  {groups.map((group) => (
                    <option key={group.id} value={String(group.id)}>
                      {group.name} · {relayRateDetail(group)}
                    </option>
                  ))}
                </select>
              </label>
              <IconButton
                label={`删除 Key ${key.name}`}
                className="danger"
                onClick={() => {setDeleteTarget(key);setDeleteScope("local");setDeleteNotice("");}}
                disabled={Boolean(busyKey || busyAction)}
              >
                {busyKey === String(key.id) ? <Loader2 className="spin" size={16} /> : <Trash2 size={16} />}
              </IconButton>
            </article>
          ))}
        </div>
        <section className="relay-manager-create-panel" aria-label="快捷创建 API Key">
          <span className="relay-manager-create-copy">
            <Plus size={18} />
            <span>
              <strong>快捷创建并导入</strong>
              <small>
                {account.dashboardSessionStored
                  ? "使用已加密保存的网页登录续期凭据，不需要保持登录窗口运行。"
                  : "这个旧账号尚未保存网页登录凭据，请先通过“添加账号 → 中转站登录”重新登录一次。"}
              </small>
            </span>
          </span>
          <label className="field">
            <span>Key 名称</span>
            <input
              value={keyName}
              onChange={(event) => setKeyName(event.target.value)}
              maxLength={80}
              disabled={Boolean(busyAction)}
            />
          </label>
          {account.adapter === "sub2api" && (
            <label className="field">
              <span>中转站分组</span>
              <select
                value={relayGroupId}
                onChange={(event) => setRelayGroupId(event.target.value)}
                disabled={Boolean(busyAction) || !groups.length}
              >
                {groups.map((group) => (
                  <option key={group.id} value={String(group.id)}>
                    {group.name} · {relayRateDetail(group)}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button
            type="button"
            className="button primary"
            onClick={createKey}
            disabled={
              Boolean(busyAction) ||
              !account.dashboardSessionStored ||
              !keyName.trim() ||
              (account.adapter === "sub2api" && !relayGroupId)
            }
          >
            {busyAction === "create" ? <Loader2 className="spin" size={16} /> : <Plus size={16} />}
            创建并导入
          </button>
        </section>
      </div>
      {deleteTarget && <Modal title={`删除 Key · ${deleteTarget.name}`} description={[account.siteName || "中转站",account.origin].filter(Boolean).join(" · ")} onClose={() => {if (!busyKey) setDeleteTarget(null);}}>
        <div className="modal-body key-delete-panel">
          <div className="key-delete-identity"><KeyRound size={18} /><strong>{deleteTarget.name}</strong><code>{deleteTarget.maskedKey}</code></div>
          <fieldset className="key-delete-options"><legend>删除范围</legend>
            <label className={deleteScope === "local" ? "selected" : ""}><input type="radio" name="delete-key-scope" value="local" checked={deleteScope === "local"} disabled={Boolean(busyKey)} onChange={() => setDeleteScope("local")} /><span><strong>仅删除本机</strong><small>网站上的 Key 继续有效，刷新不会重新导入。</small></span></label>
            <label className={deleteScope === "website" ? "selected" : ""}><input type="radio" name="delete-key-scope" value="website" checked={deleteScope === "website"} disabled={Boolean(busyKey) || !["new-api","sub2api"].includes(account.adapter)} onChange={() => setDeleteScope("website")} /><span><strong>同时删除网站和本机</strong><small>{["new-api","sub2api"].includes(account.adapter) ? "网站上的 Key 将失效，其他使用它的应用也会停止连接；本机备份不能恢复网站 Key。" : "此站点暂不支持远程删除，可在网站操作后刷新。"}</small></span></label>
          </fieldset>
          <p className="key-delete-hint">{keys.length <= 1 ? "这是最后一个已导入 Key。删除后保留账号，重新添加 Key 后即可继续使用。" : String(deleteTarget.id) === String(account.selectedKeyId) ? "删除当前 Key 后，会选择另一个已导入 Key；现有 Codex 连接可能需要重新应用。" : "其他 Key 和账号设置会保留。"}</p>
          {deleteNotice && <p className="key-delete-error" role="alert">{deleteNotice}</p>}
        </div>
        <footer className="modal-footer"><button className="button secondary" disabled={Boolean(busyKey)} onClick={() => setDeleteTarget(null)}>取消</button><button className="button danger" disabled={Boolean(busyKey)} onClick={deleteKey}>{busyKey ? <Loader2 className="spin" size={15} /> : <Trash2 size={15} />}{deleteScope === "website" ? "从网站和本机删除" : "仅从本机删除"}</button></footer>
      </Modal>}
    </Modal>
  );
}

function AddAccountModal({
  groups,
  initialTab,
  onClose,
  onDone,
  notify,
}) {
  const [tab, setTab] = useState(initialTab || "oauth");
  const defaultGroupForTab = useCallback(
    (tabId) => {
      const preferred = tabId === "api" ? "relay" : ["json", "files"].includes(tabId) ? "free" : "official";
      return groups.some((group) => group.id === preferred)
        ? preferred
        : groups[0]?.id || "official";
    },
    [groups],
  );
  const [groupId, setGroupId] = useState(() =>
    defaultGroupForTab(initialTab || "oauth"),
  );
  const [syncToApi, setSyncToApi] = useState(false);
  const [busy, setBusy] = useState(false);
  const [jsonText, setJsonText] = useState("");
  const [oauth, setOauth] = useState(null);
  const [oauthCallback, setOauthCallback] = useState("");
  const [label, setLabel] = useState("");
  const [oauthHelperInput, setOauthHelperInput] = useState(
    () => oauthLoginHelperDraft,
  );
  const [preview, setPreview] = useState(null);
  const [previewSelected, setPreviewSelected] = useState(() => new Set());
  const [previewVisible, setPreviewVisible] = useState(IMPORT_PREVIEW_PAGE);
  const [fileDragActive, setFileDragActive] = useState(false);
  const [apiMode, setApiMode] = useState("custom");
  const [apiProbe, setApiProbe] = useState(null);
  const [apiProbeBusy, setApiProbeBusy] = useState(false);
  const [apiNeedsModel, setApiNeedsModel] = useState(false);
  const apiProbeGeneration = useRef(0);
  const [apiKeyVisible, setApiKeyVisible] = useState(false);
  const [apiForm, setApiForm] = useState({
    id: "",
    name: "",
    baseUrl: "",
    presetId: "",
    portalUrl: "",
    integrationKind: "",
    key: "",
    model: "",
    envKey: "",
    modelsEndpoint: "",
    balanceEndpoint: "",
  });
  const fileRef = useRef(null);
  const fileDragDepth = useRef(0);
  const oauthStartedHere = useRef(false);

  const updateApiField = (field, value) => {
    if (busy) return;
    setApiForm((current) => ({ ...current, [field]: value }));
    if (field !== "model") {
      apiProbeGeneration.current++;
      setApiProbe(null);
      setApiNeedsModel(false);
    }
  };
  useEffect(() => () => { apiProbeGeneration.current++; }, []);

  const updateOauthHelperInput = useCallback((value) => {
    oauthLoginHelperDraft = value;
    setOauthHelperInput(value);
  }, []);

  const clearOauthHelper = useCallback(() => {
    oauthLoginHelperDraft = "";
    setOauthHelperInput("");
  }, []);

  useEffect(() => {
    api("/api/oauth/status")
      .then(async (result) => {
        setOauth(result.status);
        const belongsToHelper =
          result.status?.loginId &&
          result.status.loginId === oauthLoginHelperSessionId;
        if (belongsToHelper && ["starting", "waiting", "exchanging"].includes(result.status.status)) {
          oauthStartedHere.current = true;
        } else if (belongsToHelper && result.status.status === "completed") {
          oauthLoginHelperSessionId = "";
          clearOauthHelper();
          notify("OAuth 账号已添加，额度与模型正在后台同步");
          await onDone();
          onClose();
        }
      })
      .catch(() => {});
  }, [clearOauthHelper, notify, onClose, onDone]);

  useEffect(() => {
    if (!oauth || !["starting", "waiting", "exchanging"].includes(oauth.status))
      return undefined;
    let cancelled = false;
    let timer = null;
    let inFlight = false;
    const controller = new AbortController();
    const poll = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      try {
        const result = await api("/api/oauth/status", { signal: controller.signal });
        if (cancelled) return;
        setOauth(result.status);
        if (result.status.status === "completed" && oauthStartedHere.current) {
          cancelled = true;
          oauthStartedHere.current = false;
          oauthLoginHelperSessionId = "";
          clearOauthHelper();
          notify("OAuth 账号已添加，额度与模型正在后台同步");
          await onDone();
          onClose();
        }
      } catch {
        /* next poll */
      } finally {
        inFlight = false;
        if (!cancelled)
          timer = window.setTimeout(poll, 1400);
      }
    };
    timer = window.setTimeout(poll, 1400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [oauth?.status, notify, onClose, onDone, clearOauthHelper]);

  const openPreview = async (documents) => {
    if (!documents.length) return;
    if (documents.length > MAX_IMPORT_FILES) {
      notify(`单次最多读取 ${MAX_IMPORT_FILES} 个文件或粘贴项`, "error");
      return;
    }
    const totalBytes = documents.reduce(
      (total, document) => total + new Blob([document]).size,
      0,
    );
    if (totalBytes > MAX_IMPORT_BYTES) {
      notify("单次导入内容不能超过 24 MB", "error");
      return;
    }
    setBusy(true);
    try {
      const items = documents.map((authJson) => ({ authJson }));
      const result = await api("/api/accounts/import-preview", {
        method: "POST",
        body: JSON.stringify({ groupId, items, validateRemote: false }),
        timeoutMs: 60_000,
      });
      setPreview({ documents, data: result.preview });
      setPreviewVisible(IMPORT_PREVIEW_PAGE);
      setPreviewSelected(selectableImportPreviewIndices(result.preview.items));
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const submitFiles = async (files) => {
    if (!files.length) return;
    try {
      const selectedFiles = [...files];
      if (selectedFiles.length > MAX_IMPORT_FILES)
        throw new Error(`单次最多选择 ${MAX_IMPORT_FILES} 个文件`);
      const totalBytes = selectedFiles.reduce(
        (total, file) => total + file.size,
        0,
      );
      if (totalBytes > MAX_IMPORT_BYTES)
        throw new Error("所选文件总计超过 24 MB，请分批导入");
      const documents = [];
      for (let offset = 0; offset < selectedFiles.length; offset += 20) {
        documents.push(
          ...(await Promise.all(
            selectedFiles.slice(offset, offset + 20).map((file) => file.text()),
          )),
        );
      }
      await openPreview(documents);
    } catch (error) {
      notify(error.message, "error");
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const enterFileDropZone = (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (busy) return;
    fileDragDepth.current += 1;
    setFileDragActive(true);
  };

  const leaveFileDropZone = (event) => {
    event.preventDefault();
    event.stopPropagation();
    fileDragDepth.current = Math.max(0, fileDragDepth.current - 1);
    if (!fileDragDepth.current) setFileDragActive(false);
  };

  const dropImportFiles = (event) => {
    event.preventDefault();
    event.stopPropagation();
    fileDragDepth.current = 0;
    setFileDragActive(false);
    if (busy) return;
    const files = event.dataTransfer?.files;
    if (files?.length) submitFiles(files);
  };

  const confirmImport = async () => {
    if (!previewSelected.size) return;
    setBusy(true);
    try {
      const result = await api("/api/accounts/import-batch", {
        method: "POST",
        body: JSON.stringify({
          groupId,
          proxyEnabled: syncToApi,
          selectedIndices: [...previewSelected],
          items: preview.documents.map((authJson) => ({ authJson })),
        }),
        timeoutMs: 300_000,
      });
      const imported =
        result.result.imported.length +
        (result.result.importedProviders || []).length +
        (result.result.importedRelayAccounts || []).length;
      const skipped = (result.result.skippedDuplicates || []).length;
      const refreshing = (result.result.refreshAccountIds || []).length;
      if (result.result.failed.length)
        notify(
          `已导入 ${imported} 个，${result.result.failed.length} 个失败${skipped ? `，跳过 ${skipped} 个重复项` : ""}`,
          "error",
        );
      else
        notify(
          `已导入 ${imported} 个账号${skipped ? `，跳过 ${skipped} 个重复项` : ""}${refreshing ? "；额度与模型正在后台同步" : ""}`,
        );
      await onDone();
      if (imported) onClose();
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const startOauth = async () => {
    setBusy(true);
    try {
      oauthStartedHere.current = true;
      const result = await api("/api/oauth/start", {
        method: "POST",
        body: JSON.stringify({ groupId, label, proxyEnabled: syncToApi }),
      });
      setOauthCallback("");
      setOauth(result.status);
      oauthLoginHelperSessionId = String(result.status?.loginId || "");
      if (!result.status?.browserOpened) {
        notify("系统浏览器未能自动打开，请点击“重新打开浏览器”继续授权", "error");
      }
    } catch (error) {
      oauthStartedHere.current = false;
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const submitOauthCallback = async () => {
    if (!oauthCallback.trim()) return;
    setBusy(true);
    try {
      const result = await api("/api/oauth/callback", {
        method: "POST",
        body: JSON.stringify({ callbackUrl: oauthCallback.trim() }),
      });
      setOauth(result.status);
      notify("回调已接收，正在安全添加账号");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const reopenOauth = async () => {
    setBusy(true);
    try {
      const result = await api("/api/oauth/open", {
        method: "POST",
        body: "{}",
      });
      setOauth(result.status);
      notify("已重新打开 OpenAI 官方授权页");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const cancelOauth = async () => {
    setBusy(true);
    try {
      const result = await api("/api/oauth/cancel", {
        method: "POST",
        body: "{}",
      });
      oauthStartedHere.current = false;
      oauthLoginHelperSessionId = "";
      setOauthCallback("");
      clearOauthHelper();
      setOauth(result.status);
      notify("OAuth 授权已取消，可以重新开始");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const probeApi = async () => {
    if (!apiForm.baseUrl.trim() || !apiForm.key.trim()) {
      notify("只需填写 API 地址和 API Key，即可开始识别", "error");
      return null;
    }
    setApiProbeBusy(true);
    const generation = ++apiProbeGeneration.current;
    try {
      const result = await api("/api/api-accounts/probe", {
        method: "POST",
        body: JSON.stringify({
          baseUrl: apiForm.baseUrl,
          key: apiForm.key,
          name: apiForm.name,
          id: apiForm.id,
          portalUrl: apiForm.portalUrl,
          integrationKind: apiForm.integrationKind,
          modelsEndpoint: apiForm.modelsEndpoint,
          balanceEndpoint: apiForm.balanceEndpoint,
        }),
        timeoutMs: 90_000,
      });
      if (generation !== apiProbeGeneration.current) return null;
      setApiProbe(result.probe);
      setApiNeedsModel(Boolean(result.probe.needsModel));
      if (result.probe.status === "error") {
        notify("连接未通过认证，请检查 API 地址和 Key", "error");
      } else if (result.probe.needsModel) {
        notify("未读取到模型目录；请填写站点实际支持的模型 ID 后添加", "warning");
      } else if (result.probe.warnings?.length) {
        notify("已读取模型目录；部分站点信息未开放", "warning");
      } else {
        notify("连接、模型目录与额度信息均已识别");
      }
      return result.probe;
    } catch (error) {
      if (generation !== apiProbeGeneration.current) return null;
      setApiProbe(null);
      notify(error.message, "error");
      return null;
    } finally {
      setApiProbeBusy(false);
    }
  };

  const submitApi = async () => {
    if (!apiForm.baseUrl.trim() || !apiForm.key.trim()) {
      notify("请填写 API 地址和 API Key", "error");
      return;
    }
    if (apiNeedsModel && !apiForm.model.trim()) {
      notify("请填写此站点实际支持的模型 ID", "warning");
      return;
    }
    setBusy(true);
    try {
      const response = await api("/api/api-accounts/import", {
        method: "POST",
        body: JSON.stringify({
          ...apiForm,
          id: apiForm.id.trim() || apiProbe?.id || "",
          name: apiForm.name.trim() || apiProbe?.name || "",
          envKey: apiForm.envKey || apiProbe?.envKey || "",
          portalUrl: apiForm.portalUrl || apiProbe?.portalUrl || "",
          integrationKind:
            apiForm.integrationKind || apiProbe?.integrationKind || "",
          resolvedBaseUrl: apiProbe?.resolvedBaseUrl || "",
          modelsEndpoint:
            apiForm.modelsEndpoint || apiProbe?.modelsEndpoint || "",
          balanceEndpoint:
            apiForm.balanceEndpoint || apiProbe?.balanceEndpoint || "",
          models: apiProbe?.models || [],
          modelCapabilities: apiProbe?.modelCapabilities || {},
          model: apiForm.model || apiProbe?.preferredModel || "",
          groupId,
          fetchModels: true,
          fetchBalance: true,
          activate: false,
          proxyEnabled: syncToApi,
        }),
      });
      const discovery = response.result?.discovery;
      const balanceWarning = response.result?.balanceWarning;
      const modelCount = response.result?.models?.length || 0;
      const balanceLabel = response.result?.balance
        ? formatBalance(response.result.balance)
        : "额度接口未开放";
      const identified = `${modelCount} 个模型 · ${balanceLabel}`;
      if (discovery?.status === "stale") {
        notify(
          `${syncToApi ? "API 账号已添加并加入本地 API 号池" : "API 账号已添加"} · ${identified}；${discovery.warning}${balanceWarning ? `；${balanceWarning}` : ""}`,
          "warning",
        );
      } else if (balanceWarning) {
        notify(
          `${syncToApi ? "API 账号已添加并加入本地 API 号池" : "API 账号已添加"} · ${identified}；${balanceWarning}`,
          "warning",
        );
      } else {
        notify(
          syncToApi
            ? `API 账号已添加并加入本地 API 号池 · ${identified}`
            : `API 账号已添加 · ${identified}`,
        );
      }
      await onDone();
      onClose();
    } catch (error) {
      if (/没有可用模型|请填写模型 ID/.test(error.message)) setApiNeedsModel(true);
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="添加账号"
      description="先识别凭据与来源，再验证可用模型；额度信息以来源支持为准"
      onClose={onClose}
      wide
    >
      {preview ? (
        <div className="modal-body import-preview-panel">
          <div className="preview-heading">
            <div>
              <span className="eyebrow">IMPORT PREVIEW</span>
              <h3>确认要导入的账号</h3>
              <p>
                已识别 {preview.data.total} 个，可导入 {preview.data.valid}{" "}
                个
                {preview.data.duplicatesInBatch
                  ? `，其中 ${preview.data.duplicatesInBatch} 个为本批重复项`
                  : ""}
                {preview.data.remoteInvalid
                  ? `，已自动排除 ${preview.data.remoteInvalid} 个失效账号`
                  : ""}
                ；已完成本地格式检查，导入后在后台验证登录并同步额度与模型。
              </p>
            </div>
            <span
              className={cx("preview-count", preview.data.invalid && "warning")}
            >
              {previewSelected.size} 已选择
            </span>
          </div>
          <div className="preview-actions">
            <button
              type="button"
              onClick={() =>
                setPreviewSelected(
                  selectableImportPreviewIndices(preview.data.items),
                )
              }
              disabled={busy}
            >
              选择全部可导入
            </button>
            <button
              type="button"
              onClick={() => setPreviewSelected(new Set())}
              disabled={busy}
            >
              清空选择
            </button>
          </div>
          <div className="import-preview-list">
            {preview.data.items.slice(0, previewVisible).map((item) => {
              const selectable = isSelectableImportPreviewItem(item);
              return (
                <button
                key={item.index}
                type="button"
                className={cx(
                  "preview-item",
                  selectable && previewSelected.has(item.index) && "selected",
                  !selectable && "invalid",
                )}
                aria-pressed={
                  selectable ? previewSelected.has(item.index) : undefined
                }
                onClick={() =>
                  selectable &&
                  setPreviewSelected((current) => {
                    const next = new Set(current);
                    next.has(item.index)
                      ? next.delete(item.index)
                      : next.add(item.index);
                    return next;
                  })
                }
                disabled={!selectable || busy}
              >
                <span className="preview-check">
                  {selectable && previewSelected.has(item.index) ? (
                    <CheckSquare size={19} />
                  ) : (
                    <Square size={19} />
                  )}
                </span>
                <span className={cx("source-symbol", item.kind)}>
                  {item.kind === "provider" ? (
                    <Server size={17} />
                  ) : (
                    <Globe2 size={17} />
                  )}
                </span>
                <span className="preview-copy">
                  <strong>{item.name}</strong>
                  <small>
                    {item.email ||
                      (item.kind === "provider" ? "API 中转站" : "未提供邮箱")}
                  </small>
                  <em>
                    {item.error ||
                      [item.importFormat || (item.sourceType === "web_session" ? "Web Session" : item.kind === "provider" ? "API Key" : item.authMode === "apikey" ? "OpenAI API Key" : "Codex Auth"), (item.authMode === "chatgpt" && item.planLabel ? subscriptionBadge([item.planLabel]).label : item.planLabel), item.remoteValidation === "unverified" ? "格式已识别，导入后验证" : item.capabilityLabel, item.duplicate && "将更新已有账号", item.duplicateInBatch && "本批重复（默认跳过）", item.warning].filter(Boolean).join(" · ")}
                  </em>
                </span>
                <span className="preview-expiry">
                  {item.tokenExpiresAt ? (
                    <>
                      令牌到期
                      <strong>{formatDateTime(item.tokenExpiresAt)}</strong>
                    </>
                  ) : item.modelsCount ? (
                    <>
                      已有模型<strong>{item.modelsCount} 个</strong>
                    </>
                  ) : (
                    <strong>{item.valid ? "可导入" : "无法导入"}</strong>
                  )}
                </span>
                </button>
              );
            })}
            {previewVisible < preview.data.items.length && (
              <button
                className="session-load-more"
                onClick={() =>
                  setPreviewVisible((current) =>
                    Math.min(
                      preview.data.items.length,
                      current + IMPORT_PREVIEW_PAGE,
                    ),
                  )
                }
              >
                加载更多
                <span>剩余 {preview.data.items.length - previewVisible}</span>
              </button>
            )}
          </div>
          <div className="preview-footer">
            <button
              className="button secondary"
              onClick={() => setPreview(null)}
              disabled={busy}
            >
              返回修改
            </button>
            <button
              className="button primary"
              onClick={confirmImport}
              disabled={busy || !previewSelected.size}
            >
              {busy ? (
                <Loader2 className="spin" size={17} />
              ) : (
                <Upload size={17} />
              )}
              确认导入 {previewSelected.size} 个
            </button>
          </div>
        </div>
      ) : (
        <>
          <div
            className="add-tabs"
            role="tablist"
            style={{
              "--tab-shift": `calc(${[
                "oauth",
                "json",
                "api",
                "files",
              ].indexOf(tab) * 100}% + ${[
                "oauth",
                "json",
                "api",
                "files",
              ].indexOf(tab) * 5}px)`,
            }}
          >
            <span className="add-tab-slider" aria-hidden="true" />
            {[
              ["oauth", Globe2, "OAuth 授权"],
              ["json", KeyRound, "Token / JSON"],
              ["api", Server, "中转站 / API"],
              ["files", Upload, "批量文件"],
            ].map(([id, Icon, text]) => (
              <button
                key={id}
                className={tab === id ? "active" : ""}
                onClick={() => {
                  setTab(id);
                  setGroupId(defaultGroupForTab(id));
                }}
                role="tab"
                aria-selected={tab === id}
              >
                <Icon size={17} />
                {text}
              </button>
            ))}
          </div>
          <div className="modal-body">
            <div className="import-options">
              <label className="field compact-field">
                <span>导入到分组</span>
                <select
                  value={groupId}
                  onChange={(event) => setGroupId(event.target.value)}
                >
                  {groups.map((group) => (
                    <option value={group.id} key={group.id}>
                      {group.name}
                    </option>
                  ))}
                </select>
              </label>
              <div className="sync-api-option">
                  <div>
                    <strong>同步到 API</strong>
                    <small>
                      {tab === "api"
                        ? "导入后立即加入本地反代号池"
                        : "导入后加入本地 Web2API 账号池"}
                    </small>
                  </div>
                  <Switch
                    checked={syncToApi}
                    onChange={setSyncToApi}
                    ariaLabel="同步到 API"
                  />
              </div>
            </div>
            <AnimatedSize className="import-tab-stage">
              <div className="import-tab-scene" key={tab}>
            {tab === "oauth" && (
              <div
                className={cx(
                  "oauth-auth-layout",
                  ["starting", "waiting", "exchanging"].includes(oauth?.status) && "with-helper",
                )}
              >
                <div className="import-panel oauth-primary-panel">
                <div className="import-illustration">
                  <Globe2 size={28} />
                  <div>
                    <h3>浏览器设备授权</h3>
                    <p>
                      无需复制 Token，授权完成后自动保存账号并刷新模型与额度。
                    </p>
                  </div>
                </div>
                <label className="field">
                  <span>账号备注（可选）</span>
                  <input
                    value={label}
                    onChange={(event) => setLabel(event.target.value)}
                    placeholder="例如：工作账号"
                  />
                </label>
                {oauth?.url && (
                  <div className="oauth-box">
                    <span>OpenAI 官方授权页</span>
                    <button
                      className="oauth-open-link"
                      onClick={async () => {
                        try {
                          await copyText(oauth.url);
                          notify("授权登录网址已复制");
                        } catch (copyError) {
                          notify(copyError.message, "error");
                        }
                      }}
                      disabled={busy || oauth?.status === "exchanging"}
                      title="点击复制完整授权登录网址"
                    >
                      <Copy size={14} />
                      点击复制授权登录网址
                    </button>
                    <small>
                      {(() => {
                        try {
                          return new URL(oauth.url).hostname;
                        } catch {
                          return "openai.com";
                        }
                      })()}
                      {oauth.expiresAt
                        ? ` · ${remainingDuration(oauth.expiresAt).label}后超时`
                        : ""}
                    </small>
                    {oauth.code && (
                      <button
                        onClick={() =>
                          copyText(oauth.code).catch(error => notify(error.message, "error"))
                        }
                      >
                        <strong>{oauth.code}</strong>
                        <Copy size={15} />
                      </button>
                    )}
                  </div>
                )}
                {oauth?.status === "waiting" && (
                  <div className="oauth-manual-callback">
                    <div>
                      <strong>浏览器没有自动返回？</strong>
                      <small>
                        登录后复制浏览器地址栏里的完整 localhost 回调地址，在这里手动提交。
                      </small>
                    </div>
                    <textarea
                      value={oauthCallback}
                      onChange={(event) => setOauthCallback(event.target.value)}
                      placeholder={`http://localhost:${oauth?.port || 1455}/auth/callback?code=...&state=...`}
                      rows={3}
                    />
                    <button
                      className="button secondary full"
                      onClick={submitOauthCallback}
                      disabled={busy || !oauthCallback.trim()}
                    >
                      {busy ? <Loader2 className="spin" size={17} /> : <Check size={17} />}
                      我已授权，提交回调
                    </button>
                  </div>
                )}
                {["starting", "waiting", "exchanging"].includes(oauth?.status) ? (
                  <div className="oauth-active-actions">
                    <button
                      className="button primary"
                      onClick={reopenOauth}
                      disabled={busy || !oauth?.url || oauth?.status === "exchanging"}
                    >
                      {oauth?.status === "exchanging" || !oauth?.url ? (
                        <Loader2 className="spin" size={17} />
                      ) : (
                        <ExternalLink size={17} />
                      )}
                      {oauth?.status === "exchanging"
                        ? "正在交换凭据并添加账号"
                        : oauth?.url
                          ? "重新打开浏览器"
                          : "正在准备授权页"}
                    </button>
                    <button className="button secondary" onClick={cancelOauth} disabled={busy}>
                      <X size={17} />
                      取消授权
                    </button>
                  </div>
                ) : (
                  <button
                    className="button primary full"
                    onClick={startOauth}
                    disabled={busy}
                  >
                    {busy ? <Loader2 className="spin" size={17} /> : <Globe2 size={17} />}
                    {oauth?.status === "expired" || oauth?.status === "cancelled" || oauth?.status === "error"
                      ? "重新开始 OAuth 授权"
                      : "开始 OAuth 授权"}
                  </button>
                )}
                {["expired", "cancelled", "error"].includes(oauth?.status) && (
                  <div className={cx("oauth-status-message", oauth?.status === "error" && "error")}>
                    <AlertTriangle size={16} />
                    <div>
                      <strong>
                        {oauth.status === "expired"
                          ? "授权已超时"
                          : oauth.status === "cancelled"
                            ? "授权已取消"
                            : "授权没有完成"}
                      </strong>
                      <small>{oauth.error || "可以直接重新开始，不会再被旧任务阻塞。"}</small>
                    </div>
                  </div>
                )}
                </div>
                {["starting", "waiting", "exchanging"].includes(oauth?.status) && (
                  <OAuthTwoFactorCard
                    notify={notify}
                    input={oauthHelperInput}
                    onInputChange={updateOauthHelperInput}
                  />
                )}
              </div>
            )}
            {tab === "json" && (
              <div className="import-panel">
                <label className="field">
                  <span>粘贴账号、Token 或 API 配置</span>
                  <textarea
                    className="json-input"
                    value={jsonText}
                    onChange={(event) => setJsonText(event.target.value)}
                    placeholder={
                      '可混合粘贴多个账号：\n{ "tokens": { ... } }\n{ "type": "codex", "access_token": "...", "account_id": "..." }\n\nAPI 配置示例：\nbase_url=https://api.example.com/v1\napi_key=sk-...\n\n也支持 JSON 数组、NDJSON、嵌套导出包与网页会话。'
                    }
                  />
                </label>
                <p className="helper">
                  <Sparkles size={15} />
                  支持官方账号导出、API 地址与 Key、常见批量 JSON 和网页会话。
                  明确来源的短期 OAuth Token 可用于反代；无刷新凭据时，过期后需重新登录。
                  预览会分别标明凭据类型、可用范围和需要补充的信息。
                </p>
                <button
                  className="button primary full"
                  onClick={() => openPreview([jsonText])}
                  disabled={busy || !jsonText.trim()}
                >
                  {busy ? (
                    <Loader2 className="spin" size={17} />
                  ) : (
                    <Upload size={17} />
                  )}
                  识别账号并预览
                </button>
              </div>
            )}
            {tab === "api" && (
              <div className="import-panel form-stack">
                <div className="api-login-mode" role="tablist" aria-label="API 接入方式">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={apiMode === "custom"}
                    className={cx(apiMode === "custom" && "active")}
                    onClick={() => setApiMode("custom")}
                  >
                    <span><KeyRound size={19} /></span>
                    <span>
                      <strong>使用 API Key</strong>
                      <small>手动填写 API 地址与 Key</small>
                    </span>
                    {apiMode === "custom" && <Check size={16} />}
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={apiMode === "relay"}
                    className={cx(apiMode === "relay" && "active")}
                    onClick={() => setApiMode("relay")}
                  >
                    <span><Globe2 size={19} /></span>
                    <span>
                      <strong>中转站登录</strong>
                      <small>网页登录后自动读取账号和 Key</small>
                    </span>
                    {apiMode === "relay" && <Check size={16} />}
                  </button>
                </div>
                {apiMode === "custom" ? (
                  <div className="api-simple-flow">
                    <section className="api-simple-card">
                      <header>
                        <span><PlugZap size={20} /></span>
                        <div>
                          <strong>只填写地址和 API Key</strong>
                          <small>自动识别模型、额度、站点类型、可用 Base URL 与延迟</small>
                        </div>
                      </header>
                      <label className="field">
                        <span>API 地址</span>
                        <input
                          type="url"
                          inputMode="url"
                          value={apiForm.baseUrl}
                          onChange={(event) => updateApiField("baseUrl", event.target.value)}
                          placeholder="https://api.example.com/v1"
                          autoFocus
                          spellCheck={false}
                        />
                      </label>
                      <label className="field">
                        <span>API Key</span>
                        <div className="secret-input-wrap">
                          <input
                            type={apiKeyVisible ? "text" : "password"}
                            value={apiForm.key}
                            onChange={(event) => updateApiField("key", event.target.value)}
                            placeholder="sk-..."
                            autoComplete="new-password"
                            spellCheck={false}
                          />
                          <button
                            type="button"
                            onClick={() => setApiKeyVisible((current) => !current)}
                            aria-label={apiKeyVisible ? "隐藏 API Key" : "显示 API Key"}
                            title={apiKeyVisible ? "隐藏 API Key" : "显示 API Key"}
                          >
                            {apiKeyVisible ? <EyeOff size={16} /> : <Eye size={16} />}
                          </button>
                        </div>
                      </label>
                      <div className="api-simple-actions">
                        <button
                          type="button"
                          className="button secondary"
                          onClick={probeApi}
                          disabled={apiProbeBusy || busy || !apiForm.baseUrl.trim() || !apiForm.key.trim()}
                        >
                          {apiProbeBusy ? <Loader2 className="spin" size={17} /> : <Search size={17} />}
                          {apiProbeBusy ? "正在识别" : apiProbe ? "重新检测" : "检测连接"}
                        </button>
                        <button
                          type="button"
                          className="button primary"
                          onClick={submitApi}
                          disabled={busy || apiProbeBusy || apiProbe?.status === "error" || !apiForm.baseUrl.trim() || !apiForm.key.trim()}
                        >
                          {busy ? <Loader2 className="spin" size={17} /> : <Server size={17} />}
                          {busy ? "正在同步" : "识别并添加"}
                        </button>
                      </div>
                      {apiNeedsModel && <label className="field api-missing-model"><span>补充模型 ID</span><small>站点未开放模型目录，填写控制台列出的准确模型名即可继续。</small><input value={apiForm.model} onChange={event => updateApiField("model", event.target.value)} placeholder="填写此站点支持的模型 ID" disabled={busy} spellCheck={false} /></label>}
                    </section>

                    {apiProbe && (
                      <section
                        className={cx("api-probe-result", `status-${apiProbe.status}`)}
                        role="status"
                        aria-live="polite"
                      >
                        <header>
                          <span>{apiProbe.status === "error" ? <AlertTriangle size={19} /> : <Check size={19} />}</span>
                          <div>
                            <strong>{apiProbe.name}</strong>
                            <small>{apiProbe.resolvedBaseUrl || apiProbe.baseUrl}</small>
                          </div>
                          <em>{apiProbe.status === "ready" ? "完整识别" : apiProbe.status === "error" ? "认证失败" : "部分识别"}</em>
                        </header>
                        <div className="api-probe-facts">
                          <span><small>站点类型</small><strong>{apiProbe.integrationLabel || providerIntegrationLabel(apiProbe)}</strong></span>
                          <span><small>模型目录</small><strong>{apiProbe.models?.length || 0} 个</strong></span>
                          <span><small>剩余额度</small><strong>{apiProbe.balance ? formatBalance(apiProbe.balance) : "站点未开放"}</strong></span>
                          <span><small>响应延迟</small><strong>{typeof apiProbe.modelLatencyMs === "number" ? `${apiProbe.modelLatencyMs} ms` : "未测得"}</strong></span>
                        </div>
                        {apiProbe.balance && (
                          <div className="api-probe-usage">
                            <span><small>已用</small><strong>{formatBalanceAmount(apiProbe.balance.used, apiProbe.balance)}</strong></span>
                            <span><small>总额度</small><strong>{apiProbe.balance.unlimited ? "不限额度" : formatBalanceAmount(apiProbe.balance.limit, apiProbe.balance)}</strong></span>
                            <span><small>套餐 / 模式</small><strong>{apiProbe.balance.planName || (apiProbe.balance.mode === "unrestricted" ? "不限额" : "按额度")}</strong></span>
                          </div>
                        )}
                        {!!apiProbe.models?.length && (
                          <div className="api-probe-models" aria-label="已识别模型">
                            {apiProbe.models.slice(0, 8).map((model) => <code key={model}>{model}</code>)}
                            {apiProbe.models.length > 8 && <code>+{apiProbe.models.length - 8}</code>}
                          </div>
                        )}
                        {!!apiProbe.warnings?.length && (
                          <p><AlertTriangle size={14} />{apiProbe.warnings[0]}</p>
                        )}
                      </section>
                    )}

                    <details className="api-advanced-options">
                      <summary><Settings size={16} /><span><strong>高级设置</strong><small>仅在自动识别失败或需要覆盖时填写</small></span><ChevronDown size={15} /></summary>
                      <div className="api-advanced-grid">
                        <div className="form-grid two">
                          <label className="field">
                            <span>显示名称 <small>可选</small></span>
                            <input value={apiForm.name} onChange={(event) => updateApiField("name", event.target.value)} placeholder="默认按域名生成" />
                          </label>
                          <label className="field">
                            <span>Provider ID <small>可选</small></span>
                            <input value={apiForm.id} onChange={(event) => updateApiField("id", event.target.value)} placeholder="默认按域名和 Key 指纹生成" spellCheck={false} />
                          </label>
                        </div>
                        <div className="form-grid two">
                          <label className="field">
                            <span>官网 / Key 管理页 <small>可选</small></span>
                            <input value={apiForm.portalUrl} onChange={(event) => updateApiField("portalUrl", event.target.value)} placeholder="默认使用 API 地址的站点主页" spellCheck={false} />
                          </label>
                          <label className="field">
                            <span>模型 ID <small>目录不可用时使用</small></span>
                            <input value={apiForm.model} onChange={(event) => updateApiField("model", event.target.value)} placeholder="使用此站点给出的准确模型名" spellCheck={false} />
                          </label>
                        </div>
                        <div className="form-grid two">
                          <label className="field">
                            <span>模型目录接口 <small>可选</small></span>
                            <input value={apiForm.modelsEndpoint} onChange={(event) => updateApiField("modelsEndpoint", event.target.value)} placeholder="https://example.com/v1/models" spellCheck={false} />
                          </label>
                          <label className="field">
                            <span>额度接口 <small>可选</small></span>
                            <input value={apiForm.balanceEndpoint} onChange={(event) => updateApiField("balanceEndpoint", event.target.value)} placeholder="留空自动识别 New API / Sub2API" spellCheck={false} />
                          </label>
                        </div>
                      </div>
                    </details>
                    <p className="api-key-privacy"><ShieldCheck size={15} />检测时 Key 只会发送到所填地址的同源接口；跨站跳转会被拒绝，保存后使用 Windows DPAPI 加密。</p>
                  </div>
                ) : (
                  <RelayLoginPanel
                    groupId={groupId}
                    syncToApi={syncToApi}
                    notify={notify}
                    onDone={async () => { await onDone(); onClose(); }}
                  />
                )}
              </div>
            )}
            {tab === "files" && (
              <div className="import-panel">
                <div
                  className={cx("drop-zone", fileDragActive && "drag-active")}
                  role="button"
                  tabIndex={busy ? -1 : 0}
                  aria-disabled={busy}
                  aria-label="拖入或选择一个或多个账号文件"
                  onClick={() => !busy && fileRef.current?.click()}
                  onKeyDown={(event) => {
                    if (!busy && ["Enter", " "].includes(event.key)) {
                      event.preventDefault();
                      fileRef.current?.click();
                    }
                  }}
                  onDragEnter={enterFileDropZone}
                  onDragOver={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    if (!busy && event.dataTransfer)
                      event.dataTransfer.dropEffect = "copy";
                  }}
                  onDragLeave={leaveFileDropZone}
                  onDrop={dropImportFiles}
                >
                  <Upload size={30} />
                  <strong>
                    {fileDragActive
                      ? "松开即可识别账号文件"
                      : "拖入文件，或点击选择账号文件"}
                  </strong>
                  <span>
                    最多 500 个文件、展开后 1000 个账号、总计 24 MB；支持
                    JSON、JSONL、NDJSON 与 TXT
                  </span>
                </div>
                <input
                  ref={fileRef}
                  className="visually-hidden"
                  type="file"
                  accept=".json,.jsonl,.ndjson,.txt,application/json,text/plain"
                  multiple
                  onChange={(event) => submitFiles(event.target.files)}
                />
              </div>
            )}
              </div>
            </AnimatedSize>
          </div>
        </>
      )}
    </Modal>
  );
}

function GroupModal({ group, onClose, onDone, notify, confirm }) {
  const [name, setName] = useState(group?.name || "");
  const [color, setColor] = useState(group?.color || "violet");
  const [busy, setBusy] = useState(false);
  const remove = async () => {
    if (!group?.id || group.system || busy) return;
    const approved = await confirm({
      title: `删除“${group.name}”分组？`,
      message: "组内账号和 API 会保留，并移回各自的默认分组。",
      detail: "只删除本机分组，不删除账号、Key，也不修改中转站网站的线路分组。",
      confirmLabel: "删除分组",
      tone: "danger",
    });
    if (!approved) return;
    setBusy(true);
    try {
      await api(`/api/account-groups/${encodeURIComponent(group.id)}`, { method: "DELETE" });
      await onDone({ deletedGroupId: group.id });
      notify("分组已删除，原有账号和 API 已保留");
      onClose();
    } catch (error) { notify(error.message, "error"); }
    finally { setBusy(false); }
  };
  const submit = async () => {
    setBusy(true);
    try {
      await api("/api/account-groups", {
        method: "POST",
        body: JSON.stringify({ originalId: group?.id, name, color }),
      });
      notify(group ? "分组已更新" : "分组已创建");
      await onDone();
      onClose();
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={group ? "修改分组" : "新建分组"} onClose={onClose}>
      <div className="modal-body form-stack">
        <label className="field">
          <span>分组名称</span>
          <input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="例如：测试账号"
          />
        </label>
        <div className="field">
          <span>标记颜色</span>
          <div className="color-picks">
            {["green", "amber", "blue", "violet", "cyan", "rose", "slate"].map(
              (tone) => (
                <button
                  key={tone}
                  className={cx(`tone-${tone}`, color === tone && "active")}
                  onClick={() => setColor(tone)}
                  aria-label={tone}
                >
                  <Check size={14} />
                </button>
              ),
            )}
          </div>
        </div>
        <div className="group-modal-actions">
        {group?.id && !group.system && <button className="button danger-outline" onClick={remove} disabled={busy}><Trash2 size={16} />删除分组</button>}
        <button
          className="button primary"
          onClick={submit}
          disabled={busy || !name.trim()}
        >
          {busy ? <Loader2 className="spin" size={17} /> : <Save size={17} />}
          保存分组
        </button>
        </div>
      </div>
    </Modal>
  );
}

function AccountEditModal({ item, groups, onClose, onDone, notify, onReauthenticate }) {
  const isProvider = item.kind === "provider";
  const record = item.record;
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState(() => ({
    label: record.label || record.email || item.name || "",
    name: record.name || item.name || "",
    baseUrl: record.baseUrl || "",
    presetId: record.presetId || "",
    portalUrl: record.portalUrl || "",
    integrationKind: record.integrationKind || "",
    key: "",
    modelsEndpoint: record.modelsEndpoint || "",
    balanceEndpoint: record.balanceEndpoint || "",
    models: (record.models || []).join("\n"),
    groupId: item.groupId || record.groupId || (isProvider ? "relay" : "official"),
  }));
  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    try {
      if (isProvider) {
        const models = form.models
          .split(/[\n,]/)
          .map((value) => value.trim())
          .filter(Boolean);
        await api("/api/providers", {
          method: "POST",
          body: JSON.stringify({
            originalId: record.id,
            id: record.id,
            name: form.name,
            baseUrl: form.baseUrl,
            presetId: form.presetId,
            portalUrl: form.portalUrl,
            integrationKind: form.integrationKind,
            envKey: record.envKey,
            key: form.key,
            modelsEndpoint: form.modelsEndpoint,
            balanceEndpoint: form.balanceEndpoint,
            models,
            groupId: form.groupId,
          }),
        });
        notify(
          item.active
            ? "中转站已更新；请重新点击播放按钮，以事务方式应用到 Codex"
            : "中转站配置已更新",
        );
      } else {
        await api(`/api/accounts/${encodeURIComponent(record.id)}/metadata`, {
          method: "POST",
          body: JSON.stringify({ label: form.label, groupId: form.groupId }),
        });
        notify("账号显示设置已更新");
      }
      await onDone();
      onClose();
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={isProvider ? "编辑中转站 API" : "编辑账号"}
      description={
        isProvider
          ? "更新地址、模型或凭据；API Key 留空会保留当前加密凭据"
          : "设置本地显示名称与分组"
      }
      onClose={onClose}
      wide={isProvider}
      className="account-edit-modal"
    >
      <form className="modal-body form-stack account-edit-form" onSubmit={submit}>
        {isProvider ? (
          <>
            <div className="form-grid two">
              <label className="field">
                <span>显示名称</span>
                <input autoFocus value={form.name} onChange={(event) => update("name", event.target.value)} placeholder="例如：团队中转站" />
              </label>
              <label className="field">
                <span>所在分组</span>
                <select value={form.groupId} onChange={(event) => update("groupId", event.target.value)}>
                  {groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
                </select>
              </label>
            </div>
            <div className="form-grid two">
              <label className="field">
                <span>OpenAI 兼容 API 地址</span>
                <input value={form.baseUrl} onChange={(event) => update("baseUrl", event.target.value)} placeholder="https://api.example.com 或 https://api.example.com/v1" spellCheck={false} />
              </label>
              <label className="field">
                <span>官网 / Key 管理页 <small>可选</small></span>
                <input value={form.portalUrl} onChange={(event) => update("portalUrl", event.target.value)} placeholder="https://example.com/dashboard" spellCheck={false} />
              </label>
            </div>
            <div className="form-grid two">
              <label className="field">
                <span>API Key <small>留空保留原 Key</small></span>
                <input type="password" value={form.key} onChange={(event) => update("key", event.target.value)} placeholder={record.keyConfigured ? "已加密保存；无需重复输入" : "请输入 API Key"} autoComplete="new-password" />
              </label>
              <label className="field">
                <span>模型目录接口 <small>可选</small></span>
                <input value={form.modelsEndpoint} onChange={(event) => update("modelsEndpoint", event.target.value)} placeholder="https://api.example.com/v1/models" spellCheck={false} />
              </label>
            </div>
            <label className="field">
              <span>余额接口 <small>可选</small></span>
              <input value={form.balanceEndpoint} onChange={(event) => update("balanceEndpoint", event.target.value)} placeholder="https://api.example.com/dashboard/billing/credit_grants" spellCheck={false} />
            </label>
            <label className="field">
              <span>可用模型 <small>每行或逗号分隔</small></span>
              <textarea className="provider-model-editor" value={form.models} onChange={(event) => update("models", event.target.value)} placeholder="gpt-5.6-sol\ngpt-5.6-terra" spellCheck={false} />
            </label>
          </>
        ) : (
          <>
            <label className="field">
              <span>账号名称 / 备注</span>
              <input autoFocus value={form.label} onChange={(event) => update("label", event.target.value)} placeholder={record.email || "输入本地显示名称"} />
            </label>
            <label className="field">
              <span>所在分组</span>
              <RefinedSelect variant="field" ariaLabel="账号所在分组" value={form.groupId} onChange={value => update("groupId", value)} options={groups.map(group => ({value:group.id, label:group.name}))} />
            </label>
            <div className="account-edit-readonly">
              <ShieldCheck size={17} />
              <span><strong>{record.email || record.name || "已加密账号"}</strong><small>刷新会尝试自动续期；若网站已撤销登录授权，再重新登录或导入同一账号。</small></span>
              {record.authMode === "chatgpt" && record.sourceType === "codex_auth" && <button type="button" className="button secondary compact" onClick={onReauthenticate}><KeyRound size={15}/>重新认证</button>}
            </div>
          </>
        )}
        <div className="modal-actions">
          <button type="button" className="button secondary" onClick={onClose} disabled={busy}>取消</button>
          <button className="button primary" disabled={busy || (isProvider ? !form.name.trim() || !form.baseUrl.trim() || !form.models.trim() : !form.label.trim())}>
            {busy ? <Loader2 className="spin" size={16} /> : <Save size={16} />}
            保存修改
          </button>
        </div>
      </form>
    </Modal>
  );
}

const poolStrategyLabels = {
  ordered: {
    name: "顺序消耗",
    hint: "优先列表前面的来源；已有会话保持身份，只在允许安全回退时尝试下一来源",
  },
  quota_first: {
    name: "额度优先",
    hint: "优先较空闲的账号，同等负载下参考周剩余额度；已有会话保持原来源",
  },
  round_robin: { name: "轮询均衡", hint: "按模型轮转并优先较空闲来源；同一会话保持原来源" },
};

function ApiPoolCard({ data, onOpen }) {
  const config = data.settings.web2api || {};
  const status = data.web2apiStatus;
  const strategy =
    poolStrategyLabels[config.routing] || poolStrategyLabels.ordered;
  const liveQuota = status.lastQuota;
  const members = (config.accountIds?.length || 0) + (config.providerIds?.length || 0);
  return (
    <button type="button" className="api-pool-card" data-card-pool-drop="true" onClick={onOpen}>
      <span className="api-pool-symbol">
        <Server size={24} />
      </span>
      <span className="api-pool-copy">
        <small>LOCAL REVERSE API · 账号池</small>
        <strong>本地反代 API</strong>
        <em>
          {status.activeAccountId
            ? `单账号转换 · ${status.activeAccount?.label || "Web Session"}`
            : liveQuota?.remainingPercent == null
            ? `${strategy.name} · ${members} 个成员 · ${status.requestCount || 0} 次请求`
            : `Codex 周额度 ${Math.round(liveQuota.remainingPercent)}% · ${status.lastAccount?.label || "当前账号"}`}
        </em>
      </span>
      <span className="api-pool-end">
        <i className={status.running ? "online" : ""} />
        {status.activeForCodex
          ? status.activeAccountId
            ? "Codex 单账号使用"
            : "Codex 正在使用"
          : status.running
            ? "运行中"
            : "已停止"}
        <ChevronRight size={17} />
      </span>
    </button>
  );
}

function ApiPoolModal({ data, updateData, notify, confirm, onClose }) {
  const config = data.settings.web2api || {};
  const [order, setOrder] = useState(() =>
    config.sourceOrder?.length
      ? [...config.sourceOrder]
      : [
          ...(config.accountIds || []).map((id) => `account:${id}`),
          ...(config.providerIds || []).map((id) => `provider:${id}`),
        ],
  );
  const [routing, setRouting] = useState(config.routing || "ordered");
  const [port, setPort] = useState(Number(config.port || 17860));
  const [saving, setSaving] = useState(false);
  const [dragged, setDragged] = useState("");
  const accounts = Object.fromEntries(
    data.settings.accounts.map((account) => [account.id, account]),
  );
  const providers = Object.fromEntries(
    data.settings.providers.map((provider) => [provider.id, provider]),
  );
  const members = order
    .map((sourceId) => {
      const [kind, ...rest] = sourceId.split(":");
      const id = rest.join(":");
      const record = kind === "provider" ? providers[id] : accounts[id];
      return record ? { sourceId, kind, id, record } : null;
    })
    .filter(Boolean);
  const patchPool = (nextOrder, nextRouting = routing, nextPort = port) =>
    updateData((current) => {
      const accountIds = nextOrder
        .filter((id) => id.startsWith("account:"))
        .map((id) => id.slice(8));
      const providerIds = nextOrder
        .filter((id) => id.startsWith("provider:"))
        .map((id) => id.slice(9));
      return {
      ...current,
      settings: {
        ...current.settings,
        accounts: current.settings.accounts.map((account) => ({
          ...account,
          proxyEnabled: accountIds.includes(account.id),
        })),
        providers: current.settings.providers.map((provider) => ({
          ...provider,
          proxyEnabled: providerIds.includes(provider.id),
        })),
        web2api: {
          ...current.settings.web2api,
          port: Number(nextPort),
          accountIds,
          providerIds,
          sourceOrder: nextOrder,
          routing: nextRouting,
        },
      },
      web2apiStatus: {
        ...current.web2apiStatus,
        memberCount: nextOrder.length,
        routing: nextRouting,
      },
      };
    });
  const saveConfig = async (
    nextOrder,
    nextRouting = routing,
    successMessage = "API 号池顺序已保存",
    nextPort = port,
  ) => {
    if (saving) return false;
    const previousOrder = order;
    const previousRouting = routing;
    setOrder(nextOrder);
    setRouting(nextRouting);
    patchPool(nextOrder, nextRouting, nextPort);
    setSaving(true);
    try {
      const result = await api("/api/web2api/config", {
        method: "POST",
        body: JSON.stringify({
          port: Number(nextPort),
          routing: nextRouting,
          accountIds: nextOrder
            .filter((id) => id.startsWith("account:"))
            .map((id) => id.slice(8)),
          providerIds: nextOrder
            .filter((id) => id.startsWith("provider:"))
            .map((id) => id.slice(9)),
          sourceOrder: nextOrder,
        }),
      });
      updateData((current) => ({
        ...current,
        web2apiStatus: {
          ...current.web2apiStatus,
          ...(result.status || {}),
        },
      }));
      notify(successMessage);
      return true;
    } catch (error) {
      setOrder(previousOrder);
      setRouting(previousRouting);
      patchPool(previousOrder, previousRouting, config.port || 17860);
      setPort(Number(config.port || 17860));
      notify(error.message, "error");
      return false;
    } finally {
      setSaving(false);
    }
  };
  const move = (fromId, toId) => {
    if (!fromId || fromId === toId) return;
    const next = [...order];
    const from = next.indexOf(fromId);
    const to = next.indexOf(toId);
    if (from < 0 || to < 0) return;
    next.splice(to, 0, next.splice(from, 1)[0]);
    saveConfig(next);
  };
  const nudge = (id, delta) => {
    const index = order.indexOf(id);
    const target = index + delta;
    if (index < 0 || target < 0 || target >= order.length) return;
    const next = [...order];
    [next[index], next[target]] = [next[target], next[index]];
    saveConfig(next);
  };
  const changeService = async (action) => {
    setSaving(true);
    try {
      const result = await api(
        action === "stop" ? "/api/web2api/stop" : "/api/web2api/start",
        { method: "POST", body: "{}" },
      );
      updateData((current) => ({
        ...current,
        web2apiStatus: { ...current.web2apiStatus, ...result.status },
      }));
      notify(
        result.keptForSubagents
          ? "已退出主模型反代；本地路由将为子代理继续运行，彻底退出软件时才停止"
          : action === "stop"
          ? data.web2apiStatus.activeForCodex
            ? "已停止反代并用直连配置重新打开 Codex"
            : "本地 API 已停止"
          : "反代已启动，Codex 已自动使用 API 号池重新打开",
      );
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setSaving(false);
    }
  };
  const changeServiceOnly = async (action) => {
    setSaving(true);
    try {
      const result = await api(
        action === "stop"
          ? "/api/web2api/service-stop"
          : "/api/web2api/service-start",
        { method: "POST", body: "{}" },
      );
      updateData((current) => ({
        ...current,
        web2apiStatus: { ...current.web2apiStatus, ...(result.status || {}) },
      }));
      notify(action === "stop" ? "本地 API 已停止" : "本地 API 已启动；尚未修改 Codex");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setSaving(false);
    }
  };
  const rotateKey = async () => {
    const approved = await confirm({
        title: "轮换本地 API Key？",
        message: "旧 Key 将立即失效，使用公开 API 的应用需要更新 Key。",
        detail: "Codex 使用独立的内部凭据，当前任务会继续运行。",
        confirmLabel: "轮换并复制",
      });
    if (!approved) return;
    setSaving(true);
    try {
      const result = await api("/api/web2api/rotate-key", {
        method: "POST",
        body: "{}",
      });
      await copyText(result.apiKey);
      updateData((current) => ({
        ...current,
        web2apiStatus: { ...current.web2apiStatus, ...(result.status || {}) },
      }));
      notify("新 API Key 已复制；旧 Key 已失效");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setSaving(false);
    }
  };
  return (
    <Modal
      title="本地反代 API 号池"
      description="拖动账号或 API Provider 调整优先级；顺序即时生效，不会重启服务"
      onClose={onClose}
      wide
    >
      <p className="pool-access-scope"><ShieldCheck size={16} />复制的 Key 仅访问下方号池成员；主模型与子代理的独立账号选择不会对外开放。</p>
      <div className="pool-overview">
        <div>
          <span
            className={cx(
              "pool-live-dot",
              data.web2apiStatus.running && "online",
            )}
          />
          <span>
            <strong>
              {data.web2apiStatus.activeForCodex
                ? data.web2apiStatus.activeAccountId
                  ? `Codex 已锁定 ${data.web2apiStatus.activeAccount?.label || "所选 Web Session"}`
                  : "Codex 正在使用号池"
                : data.web2apiStatus.running
                  ? "API 服务运行中"
                  : "服务已停止"}
            </strong>
            <small>{data.web2apiStatus.url}/v1</small>
          </span>
        </div>
        <button
          className="button subtle"
          onClick={async () => {
            try {
              await copyText(`${data.web2apiStatus.url}/v1`);
              notify("API Base URL 已复制");
            } catch (error) { notify(error.message, "error"); }
          }}
        >
          <Copy size={16} />
          复制地址
        </button>
        <label className="pool-port-control">
          <span>监听端口</span>
          <input
            type="number"
            min="1024"
            max="65535"
            value={port}
            onChange={(event) => setPort(Number(event.target.value || 0))}
          />
          <button
            type="button"
            className="button subtle compact"
            disabled={saving || port < 1024 || port > 65535 || port === Number(config.port || 17860)}
            onClick={() => saveConfig(order, routing, "本地 API 端口已保存", port)}
          >
            保存端口
          </button>
        </label>
      </div>
      <div className="pool-service-actions" aria-label="本地 API 服务操作">
        <button className="button secondary" disabled={saving} onClick={async () => {
          setSaving(true);
          try {
            const result = await api("/api/web2api/key");
            await copyText(result.apiKey);
            notify("API Key 已复制");
          } catch (error) { notify(error.message, "error"); }
          finally { setSaving(false); }
        }}><Copy size={15} />复制 Key</button>
        <button className="button secondary" disabled={saving} onClick={rotateKey}>
          <KeyRound size={15} />轮换并复制 Key
        </button>
        <button className="button secondary" disabled={saving || !config.accountIds?.length} onClick={async () => {
          setSaving(true);
          try {
            const result = await api("/api/web2api/clear-cooldowns", { method: "POST", body: JSON.stringify({ accountIds: config.accountIds }) });
            updateData(current => ({ ...current, web2apiStatus: { ...current.web2apiStatus, ...result.status } }));
            notify("本地冷却已清除，下次请求会重新检查账号；网站额度不会重置");
          } catch (error) { notify(error.message, "error"); }
          finally { setSaving(false); }
        }}><RotateCcw size={15} />清除本地冷却</button>
        {!data.web2apiStatus.running && (
          <button className="button secondary" disabled={saving || !members.length} onClick={() => changeServiceOnly("start")}>
            <Play size={15} />仅启动 API
          </button>
        )}
        {data.web2apiStatus.running && !data.web2apiStatus.activeForCodex && (
          <button className="button danger-outline" disabled={saving} onClick={() => changeServiceOnly("stop")}>
            <X size={15} />停止 API
          </button>
        )}
        <button
          className={cx("button", data.web2apiStatus.activeForCodex ? "danger-outline" : "primary")}
          disabled={saving || (!data.web2apiStatus.activeForCodex && !members.length)}
          onClick={() => changeService(data.web2apiStatus.activeForCodex ? "stop" : "start")}
        >
          {saving ? <Loader2 className="spin" size={15} /> : data.web2apiStatus.activeForCodex ? <RotateCcw size={15} /> : <Zap size={15} />}
          {data.web2apiStatus.activeForCodex ? "停止并恢复直连" : "用于 Codex"}
        </button>
      </div>
      <div className="codex-quota-feedback">
        <CircleGauge size={18} />
        <span>
          <strong>Codex 内额度回传</strong>
          <small>
            {data.web2apiStatus.lastQuota?.remainingPercent == null
              ? "首次完成模型请求后，将按实际选中的账号显示周额度"
              : `${data.web2apiStatus.lastAccount?.label || "当前账号"} · 周额度剩余 ${Math.round(data.web2apiStatus.lastQuota.remainingPercent)}%${data.web2apiStatus.lastQuota.resetAt ? ` · ${formatTime(data.web2apiStatus.lastQuota.resetAt)} 重置` : ""}`}
          </small>
        </span>
        <em>通过 Codex 原生 x-codex 限额字段实时回传</em>
      </div>
      <PoolProtocols capabilities={data.web2apiStatus.protocolCapabilities} />
      <div
        className="pool-strategies"
        role="radiogroup"
        aria-label="账号消耗策略"
      >
        {Object.entries(poolStrategyLabels).map(([id, meta]) => (
          <button
            className={routing === id ? "active" : ""}
            role="radio"
            aria-checked={routing === id}
            key={id}
            onClick={() =>
              id !== routing && saveConfig(order, id, `已切换为${meta.name}`)
            }
          >
            <span>{routing === id && <Check size={13} />}</span>
            <strong>{meta.name}</strong>
            <small>{meta.hint}</small>
          </button>
        ))}
      </div>
      <div className="pool-list-heading">
        <div>
          <ListOrdered size={17} />
          <span>
            <strong>号池优先级</strong>
            <small>顶部成员优先使用</small>
          </span>
        </div>
        <em>{members.length} 个成员</em>
      </div>
      <div className="pool-member-list">
        {members.map((member, index) => {
          const account = member.record;
          const provider = member.kind === "provider";
          const displayName = provider
            ? account.name || account.label || member.id
            : account.name || account.label || account.email;
          return (
          <article
            className={cx("pool-member", dragged === member.sourceId && "dragging")}
            key={member.sourceId}
            draggable={!saving}
            onDragStart={() => setDragged(member.sourceId)}
            onDragEnd={() => setDragged("")}
            onDragOver={(event) => event.preventDefault()}
            onDrop={() => {
              move(dragged, member.sourceId);
              setDragged("");
            }}
          >
            <span className="drag-handle" title="拖动排序">
              <GripVertical size={18} />
            </span>
            <b>{index + 1}</b>
            <span className="pool-member-copy">
              <strong>{displayName}</strong>
              <small>
                {provider
                  ? `${account.baseUrl || "API Provider"} · ${(account.models || []).length} 个模型`
                  : `${account.email || "ChatGPT 账号"} · ${account.planLabel || account.plan || "套餐待识别"}`}
              </small>
            </span>
            <span className="pool-member-quota">
              {provider ? "API 余额" : "周额度"}{" "}
              <strong>
                {provider
                  ? formatBalance(account.balance)
                  : account.usage?.weekly?.remainingPercent == null
                    ? "--"
                    : `${Math.round(account.usage.weekly.remainingPercent)}%`}
              </strong>
            </span>
            <span className="pool-move-buttons">
              <IconButton
                label="上移"
                disabled={saving || index === 0}
                onClick={() => nudge(member.sourceId, -1)}
              >
                <ArrowUp size={15} />
              </IconButton>
              <IconButton
                label="下移"
                disabled={saving || index === members.length - 1}
                onClick={() => nudge(member.sourceId, 1)}
              >
                <ArrowDown size={15} />
              </IconButton>
              <IconButton
                label="移出 API 号池"
                className="danger"
                disabled={saving}
                onClick={() =>
                  saveConfig(
                    order.filter((id) => id !== member.sourceId),
                    routing,
                    `${displayName} 已移出 API 号池`,
                  )
                }
              >
                <X size={15} />
              </IconButton>
            </span>
          </article>
          );
        })}
        {!members.length && (
          <div className="pool-empty">
            <Server size={25} />
            <strong>API 号池还是空的</strong>
            <span>回到账号卡片加入，或使用“批量操作 → 加入 API”。</span>
          </div>
        )}
      </div>
    </Modal>
  );
}

function AccountExportModal({ item, payload, onClose, notify }) {
  const [copied, setCopied] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(null);
  const jsonText = useMemo(
    () => (payload ? JSON.stringify(payload, null, 2) : ""),
    [payload],
  );

  const copy = async () => {
    try {
      await copyText(jsonText);
      setCopied(true);
      notify("账号 JSON 已复制到剪贴板");
      window.setTimeout(() => setCopied(false), 1800);
    } catch (error) {
      notify(error.message, "error");
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      const path =
        item.kind === "account"
          ? `/api/accounts/${encodeURIComponent(item.recordId)}/export-download`
          : item.kind === "relay"
            ? `/api/relay-accounts/${encodeURIComponent(item.recordId)}/export-download`
            : `/api/providers/${encodeURIComponent(item.recordId)}/export-download`;
      const result = await api(path, { method: "POST", body: "{}" });
      setSaved(result.result);
      notify(`已保存到下载目录：${result.result.fileName}`);
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={`导出 ${item.name}`}
      description={payload?.codex_agent_manager ? "标准 auth.json 格式，可导入本软件或 Cockpit；已排除本机路由配置" : "先检查或复制 JSON，再保存为可直接导入的账号文件"}
      onClose={onClose}
      wide
    >
      <div className="account-export-body">
        <div className="account-export-warning" role="note">
          <AlertTriangle size={20} />
          <span>
            <strong>文件包含完整登录凭据</strong>
            <small>
              拿到 JSON 的人可以直接使用该账号。请只复制或发送给可信的人，文件不会自动加密。
            </small>
          </span>
        </div>

        <section className="account-export-preview">
          <div className="account-export-heading">
            <span>
              <strong>可复制的账号 JSON</strong>
              <small>可全选文本手动复制，也可以使用右侧按钮。</small>
            </span>
            <button
              type="button"
              className={cx("button", copied ? "success" : "secondary")}
              onClick={copy}
              disabled={!jsonText}
            >
              {copied ? <Check size={16} /> : <Copy size={16} />}
              {copied ? "已复制" : "复制 JSON"}
            </button>
          </div>
          <textarea
            className="account-export-json"
            aria-label={`${item.name} 的账号 JSON`}
            value={jsonText}
            readOnly
            spellCheck={false}
            onFocus={(event) => event.currentTarget.select()}
          />
        </section>

        <section className="account-export-save">
          <span className="account-export-save-icon">
            <Download size={21} />
          </span>
          <span>
            <strong>导出到系统下载目录</strong>
            <small>
              默认保存到 Windows 的“下载”文件夹；同名文件会自动编号，不会覆盖已有文件。
            </small>
            {saved?.path && <code title={saved.path}>{saved.path}</code>}
          </span>
          <button
            type="button"
            className="button primary"
            onClick={save}
            disabled={saving || !payload}
          >
            {saving ? (
              <Loader2 className="spin" size={16} />
            ) : saved ? (
              <Check size={16} />
            ) : (
              <Download size={16} />
            )}
            {saving ? "正在导出" : saved ? "已导出，可再次保存" : "导出 JSON"}
          </button>
        </section>
      </div>
      <footer className="modal-footer account-export-footer">
        <button type="button" className="button secondary" onClick={onClose}>
          完成
        </button>
      </footer>
    </Modal>
  );
}

const SWITCH_PHASE_GROUPS = [
  {
    id: "preflight",
    label: "预检",
    hint: "账号、凭据、模型与启动器",
    phases: ["queued", "preflight", "credentials", "prepared"],
  },
  {
    id: "apply",
    label: "安全切换",
    hint: "关闭旧实例并写入目标配置",
    phases: ["closing", "writing", "configuring"],
  },
  {
    id: "launch",
    label: "启动 Codex",
    hint: "唤起桌面应用并等待进程",
    phases: ["launching"],
  },
  {
    id: "verify",
    label: "身份回验",
    hint: "核对账号、模型和服务地址",
    phases: ["verifying", "recovering"],
  },
];

const SWITCH_PHASE_LABELS = {
  queued: "排队",
  preflight: "预检",
  credentials: "凭据校验",
  prepared: "准备完成",
  closing: "关闭旧 Codex",
  writing: "写入身份",
  configuring: "应用配置",
  launching: "启动 Codex",
  verifying: "运行时回验",
  recovering: "安全恢复",
  completed: "完成",
  failed: "未完成",
};

export function SwitchProgressModal({ operation, onClose, onRetry, retrying = false }) {
  if (!operation) return null;
  const running = operation.status === "running";
  const failed = operation.status === "error" || operation.phase === "failed";
  const completed = operation.status === "completed" || operation.phase === "completed";
  const displayedPhase = failed ? operation.failedPhase || operation.phase : operation.phase;
  const activeIndex = completed
    ? SWITCH_PHASE_GROUPS.length
    : Math.max(
        0,
        SWITCH_PHASE_GROUPS.findIndex((group) => group.phases.includes(displayedPhase)),
      );
  const elapsedSeconds = Math.max(0, Number(operation.elapsedMs || 0) / 1000);
  const timingEntries = Object.entries(operation.timings || {}).filter(
    ([, value]) => Number.isFinite(Number(value)),
  );
  return (
    <Modal
      title={failed ? "Codex 切换需要处理" : completed ? "Codex 已准备好" : `正在打开 ${operation.targetName || "Codex"}`}
      description={
        running
          ? "显示的是后端真实阶段；Agent Manager 不会随 Codex 一起关闭。"
          : failed
            ? operation.recoveryState === "unchanged"
              ? "目标配置尚未写入；原账号和配置没有变化，可直接重试。"
              : operation.recoveryState === "restored"
                ? "切换没有通过完整回验，软件已执行安全恢复。"
                : "切换未完成，请查看下方错误；现有恢复记录会保留。"
            : "所选账号和新会话配置已通过启动检查。"
      }
      onClose={onClose}
      dismissible={!running}
      className="switch-progress-modal"
    >
      <div className={cx("switch-progress-body", failed && "failed", completed && "completed")}>
        <div className="switch-progress-current" role="status" aria-live="polite">
          <span className="switch-progress-status-icon">
            {failed ? (
              <AlertTriangle size={24} />
            ) : completed ? (
              <Check size={24} />
            ) : (
              <Loader2 className="spin" size={24} />
            )}
          </span>
          <span>
            <strong>{operation.message || "正在处理"}</strong>
            <small>
              {SWITCH_PHASE_LABELS[displayedPhase] || "处理中"} · 已用时 {elapsedSeconds.toFixed(1)} 秒
            </small>
          </span>
        </div>
        <div
          className="switch-progress-track"
          role="progressbar"
          aria-valuemin="0"
          aria-valuemax="100"
          aria-valuenow={Math.max(0, Math.min(100, Number(operation.progress || 0)))}
        >
          <i style={{ width: `${Math.max(2, Math.min(100, Number(operation.progress || 0)))}%` }} />
        </div>
        <ol className="switch-progress-steps">
          {SWITCH_PHASE_GROUPS.map((group, index) => {
            const state = failed && index === activeIndex
              ? "error"
              : completed || index < activeIndex
                ? "completed"
                : index === activeIndex
                  ? "active"
                  : "pending";
            return (
              <li key={group.id} className={state}>
                <span>{state === "completed" ? <Check size={15} /> : index + 1}</span>
                <div><strong>{group.label}</strong><small>{group.hint}</small></div>
              </li>
            );
          })}
        </ol>
        {running && elapsedSeconds >= 4 && (
          <div className="switch-progress-note">
            <Clock3 size={17} />
            <span>Windows 首次唤起或旧进程退出可能稍慢；当前操作仍在推进，请不要重复点击。</span>
          </div>
        )}
        {failed && operation.error && (
          <div className="switch-progress-error"><AlertTriangle size={17} /><span>{operation.error}</span></div>
        )}
        {completed && operation.verificationScope === "app_server_probe" && <p className="switch-progress-note">新会话配置已通过检查。旧会话如仍无法打开，请到会话管理点击“一键修复”。</p>}
        {completed && operation.sessionVisibility?.message && <p className="switch-progress-note" role="status">{operation.sessionVisibility.message}{operation.sessionVisibility.partial || operation.sessionVisibility.reason === "repair_unavailable" ? " 可在会话管理中点击“一键修复”继续处理。" : ""}</p>}
        {timingEntries.length > 0 && !running && (
          <div className="switch-progress-timings">
            {timingEntries.map(([phase, value]) => (
              <span key={phase}>{SWITCH_PHASE_LABELS[phase] || phase}<b>{(Number(value) / 1000).toFixed(2)}s</b></span>
            ))}
          </div>
        )}
      </div>
      {!running && (
        <footer className="modal-footer">
          <button className="button secondary" type="button" onClick={onClose}>完成</button>
          {failed && operation.canRetry !== false && onRetry && (
            <button className="button primary" type="button" onClick={onRetry} disabled={retrying}>
              {retrying ? <Loader2 className="spin" size={16} /> : <RotateCcw size={16} />}
              {retrying ? "正在重试" : "重试切换"}
            </button>
          )}
        </footer>
      )}
    </Modal>
  );
}

function AccountsView({
  data,
  reload,
  reloadConnections,
  updateData,
  notify,
  confirm,
  setBusy,
  busy,
}) {
  const [groupId, setGroupId] = useState("all");
  const accountGridRef = useRef(null);
  const [query, setQuery] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [reauthAccount, setReauthAccount] = useState(null);
  const [modelInspector, setModelInspector] = useState(null);
  const [groupModal, setGroupModal] = useState(null);
  const [poolOpen, setPoolOpen] = useState(false);
  const [exportModal, setExportModal] = useState(null);
  const [editModal, setEditModal] = useState(null);
  const [relayManage, setRelayManage] = useState(null);
  const [relayRelogin, setRelayRelogin] = useState(null);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [pendingIds, setPendingIds] = useState(() => new Set());
  const [batchBusy, setBatchBusy] = useState(false);
  const [switchProgress, setSwitchProgress] = useState(null);
  const groups = [...data.settings.accountGroups].sort(
    (a, b) => a.sortOrder - b.sortOrder,
  );
  const groupMap = Object.fromEntries(groups.map((group) => [group.id, group]));
  const sources = data.modelSources
    .map((source) => {
      const record =
        source.kind === "account"
          ? data.settings.accounts.find((item) => item.id === source.recordId)
          : data.settings.providers.find((item) => item.id === source.recordId);
      return { ...source, record, group: groupMap[source.groupId] };
    })
    .filter((item) => item.record);
  const relayAccounts = Array.isArray(data.settings.relayAccounts)
    ? data.settings.relayAccounts
    : [];
  const relayProviderIds = new Set(
    relayAccounts.map((item) => String(item.providerId || "")).filter(Boolean),
  );
  const standaloneSources = sources.filter(
    (item) => !(item.kind === "provider" && relayProviderIds.has(String(item.recordId))),
  );
  const relayAccountRows = relayAccounts.map((account) => ({
    account,
    source: sources.find(
      (item) => item.kind === "provider" && String(item.recordId) === String(account.providerId),
    ),
  }));
  const filteredRelayAccounts = relayAccountRows.filter(
    ({ account, source }) => matchesAccount(source || {}, account, {
      groupId, query,
    }),
  );
  const filtered = standaloneSources.filter(
    (item) => matchesAccount(item, item.record, {
      groupId, query,
    }),
  );
  const cardRows = [
    ...filteredRelayAccounts.map((row, index) => ({
      kind: "relay",
      id: "relay:" + row.account.id,
      active: Boolean(row.source?.active),
      order: index,
      row,
    })),
    ...filtered.map((item, index) => ({
      kind: "source",
      id: item.id,
      active: Boolean(item.active),
      order: filteredRelayAccounts.length + index,
      item,
    })),
  ].sort(
    (left, right) =>
      (data.settings.dashboardOrder?.length
        ? (data.settings.dashboardOrder.indexOf(left.id) < 0 ? Number.MAX_SAFE_INTEGER : data.settings.dashboardOrder.indexOf(left.id))
          - (data.settings.dashboardOrder.indexOf(right.id) < 0 ? Number.MAX_SAFE_INTEGER : data.settings.dashboardOrder.indexOf(right.id))
        : Number(right.active) - Number(left.active)) || left.order - right.order,
  );
  const cardDrag = useAccountCardDrag({
    gridRef: accountGridRef, items: cardRows, groupId, busy: busy || selectionMode || batchBusy,
    onMove: async payload => {
      const response = await api("/api/dashboard/move", {method:"POST",body:JSON.stringify(payload)});
      updateData(current => applyDashboardMoveResult(current, response.result), { invalidateSnapshots: true });
    },
    onError: error => notify(error.message, "error"),
  });
  const renderedCardRows = cardDrag.previewIds
    ? cardDrag.previewIds.map(id => cardRows.find(card => card.id === id)).filter(Boolean)
    : cardRows;
  const invalidCount = standaloneSources.filter(
    (item) => (groupId === "all" || item.groupId === groupId) && item.kind === "account" && item.record.invalid,
  ).length;
  const visibleIds = visibleSourceIds(filtered, filteredRelayAccounts);
  const selectedItems = sources.filter((item) => selectedIds.has(item.id));
  const selectedChatgptAccounts = selectedItems.filter(
    (item) =>
      item.kind === "account" &&
      item.record.authMode === "chatgpt" &&
      !item.record.quotaOnly &&
      item.record.codexCompatible !== false,
  );
  const selectedApiProviders = selectedItems.filter(
    (item) =>
      item.kind === "provider" &&
      item.record.keyConfigured &&
      (item.record.models || []).length > 0,
  );

  const markPending = (id, active) =>
    setPendingIds((current) => {
      const next = new Set(current);
      active ? next.add(id) : next.delete(id);
      return next;
    });
  const patchSource = (item, changes) =>
    updateData((current) => ({
      ...current,
      settings: {
        ...current.settings,
        accounts: current.settings.accounts.map((record) =>
          item.kind === "account" && record.id === item.recordId
            ? { ...record, ...changes }
            : record,
        ),
        providers: current.settings.providers.map((record) =>
          item.kind === "provider" && record.id === item.recordId
            ? { ...record, ...changes }
            : record,
        ),
      },
      modelSources: current.modelSources.map((source) =>
        source.id === item.id
          ? {
              ...source,
              ...changes,
              groupName: changes.groupId
                ? groupMap[changes.groupId]?.name || source.groupName
                : source.groupName,
            }
          : source,
      ),
    }));
  const patchRelayAccountGroup = (
    account,
    source,
    nextGroupId,
    saved = null,
  ) => {
    const relayAccountId = String(account.id || "");
    const providerId = String(
      saved?.account?.providerId ||
        account.providerId ||
        saved?.provider?.id ||
        source?.recordId ||
        "",
    );
    const resolvedGroupId = String(
      saved?.account?.groupId || saved?.provider?.groupId || nextGroupId,
    );
    const resolvedGroupName =
      groupMap[resolvedGroupId]?.name || source?.groupName || "未分组";
    updateData((current) => ({
      ...current,
      settings: {
        ...current.settings,
        relayAccounts: (current.settings.relayAccounts || []).map((record) =>
          String(record.id || "") === relayAccountId
            ? saved?.account
              ? { ...record, ...saved.account }
              : { ...record, groupId: resolvedGroupId }
            : record,
        ),
        providers: current.settings.providers.map((record) =>
          providerId && String(record.id || "") === providerId
            ? saved?.provider
              ? { ...record, ...saved.provider }
              : { ...record, groupId: resolvedGroupId }
            : record,
        ),
      },
      modelSources: current.modelSources.map((modelSource) =>
        (source?.id && modelSource.id === source.id) ||
        (providerId &&
          modelSource.kind === "provider" &&
          String(modelSource.recordId || "") === providerId)
          ? {
              ...modelSource,
              groupId: resolvedGroupId,
              groupName: resolvedGroupName,
            }
          : modelSource,
      ),
    }));
  };
  const act = async (fn, reloadAfter = true) => {
    setBusy(true);
    try {
      await fn();
    } catch (error) {
      notify(error.message, "error");
    } finally {
      if (reloadAfter) await reload().catch(error => notify(error.message, "error"));
      setBusy(false);
    }
  };
  const runSwitchRequest = async (item, path) => {
    const operationId = globalThis.crypto?.randomUUID?.() ||
      `switch_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
    const startedAt = Date.now();
    let pollInFlight = false;
    setSwitchProgress({
      operationId,
      targetId: item.recordId,
      targetName: item.name,
      targetKind: item.kind,
      status: "running",
      phase: "queued",
      progress: 1,
      message: "已收到切换请求，正在准备",
      elapsedMs: 0,
      timings: {},
    });
    const poll = async () => {
      setSwitchProgress((current) =>
        current?.operationId === operationId && current.status === "running"
          ? { ...current, elapsedMs: Date.now() - startedAt }
          : current,
      );
      if (pollInFlight) return;
      pollInFlight = true;
      try {
        const payload = await api(
          `/api/switch-operation?operationId=${encodeURIComponent(operationId)}`,
          { timeoutMs: 3_000 },
        );
        setSwitchProgress((current) =>
          current?.operationId === operationId
            ? { ...current, ...payload.operation, operationId }
            : current,
        );
      } catch {
        // The POST can reach the handler a fraction after the first poll. The
        // authoritative operation response or the next poll will fill this in.
      } finally {
        pollInFlight = false;
      }
    };
    const pollTimer = window.setInterval(poll, 350);
    try {
      const response = await api(path, {
        method: "POST",
        body: JSON.stringify({ operationId, forceReapply: Boolean(item.active) }),
      });
      await poll();
      const visibilityResult = response.result?.sessionSync?.visibility;
      const sessionVisibility = visibilityResult ? {...visibilityResult, ...visibilityResult.completion} : null;
      setSwitchProgress((current) =>
        current?.operationId === operationId
          ? {
              ...current,
              status: "completed",
              phase: "completed",
              progress: 100,
              message: response.result?.launch?.readiness?.message || "已核对当前账号与新会话配置",
              verificationScope: response.result?.launch?.readiness?.verificationScope,
              sessionVisibility,
              elapsedMs: response.result?.performance?.totalMs ?? current.elapsedMs,
              timings: response.result?.performance?.stages ?? current.timings,
            }
          : current,
      );
      if (!sessionVisibility?.partial && sessionVisibility?.reason !== "repair_unavailable") {
        window.setTimeout(
          () => setSwitchProgress((current) => current?.operationId === operationId ? null : current),
          2_500,
        );
      }
      return response;
    } catch (error) {
      await poll();
      setSwitchProgress((current) =>
        current?.operationId === operationId
          ? {
              ...current,
              status: "error",
              phase: "failed",
              progress: 100,
              message: current.message || "切换未完成，原账号与配置已安全恢复",
              error: current.error || error.message,
              elapsedMs: Math.max(Number(current.elapsedMs || 0), Date.now() - startedAt),
            }
          : current,
      );
      throw error;
    } finally {
      window.clearInterval(pollTimer);
    }
  };
  const select = (item) =>
    act(async () => {
      if (item.kind === "account") {
        if (item.record.quotaOnly || item.record.codexCompatible === false)
          throw new Error(
            "该 Web Session 仅支持额度查询，不包含可用于 Codex 对话的 OAuth 凭据。",
          );
        const result = await runSwitchRequest(
          item,
          `/api/accounts/${encodeURIComponent(item.recordId)}/switch`,
        );
        if (!result.result?.verified)
          throw new Error("账号与 Codex 运行时没有完成双向回验，未确认切换成功。");
        notify(
          result.result?.mode === "web_session_local_conversion"
            ? `已通过本地转换启用 ${item.name}，Codex 已重新打开`
            : `已切换到 ${item.name}，Codex 已使用该账号重新打开`,
        );
      } else {
        const result = await runSwitchRequest(
          item,
          `/api/providers/${encodeURIComponent(item.recordId)}/switch`,
        );
        if (!result.result?.verified)
          throw new Error("中转站配置没有通过 Codex 启动回验，未确认切换成功。");
        notify(`已切换到 ${item.name} · ${result.result.model}，Codex 已验证并重新打开`);
      }
    });
  const refreshProviderMetadata = async (providerId) => {
    const [modelsResult, balanceResult] = await Promise.allSettled([
      api(`/api/providers/${encodeURIComponent(providerId)}/models`, {
        method: "POST",
        body: "{}",
      }),
      api(`/api/providers/${encodeURIComponent(providerId)}/balance`, {
        method: "POST",
        body: "{}",
      }),
    ]);
    if (modelsResult.status === "rejected") throw modelsResult.reason;
    return {
      discovery: modelsResult.value.discovery,
      balance:
        balanceResult.status === "fulfilled" ? balanceResult.value.balance : null,
      balanceWarning:
        balanceResult.status === "rejected"
          ? balanceResult.reason?.message || "额度同步失败"
          : balanceResult.value.balance
            ? ""
            : "未检测到兼容的额度接口",
    };
  };
  const refresh = async (item) => {
    markPending(item.id, true);
    try {
      if (item.kind === "account") {
        const result = await api(
          `/api/accounts/${encodeURIComponent(item.recordId)}/refresh`,
          { method: "POST", body: "{}" },
        );
        const notice = accountRefreshNotice(result.account, item.name);
        notify(notice.message, notice.kind);
        return;
      } else {
        const result = await refreshProviderMetadata(item.recordId);
        if (result.discovery?.status === "stale") {
          notify(
            `${item.name}：${result.discovery.warning}${result.balanceWarning ? `；${result.balanceWarning}` : ""}`,
            "warning",
          );
          return;
        }
        if (result.balanceWarning) {
          notify(`${item.name} 的模型已刷新；${result.balanceWarning}`, "warning");
          return;
        }
      }
      notify(`${item.name} 的模型与额度已刷新`);
        } catch (error) {
      notify(error.message, "error");
    } finally {
      await reloadConnections().catch(error => notify(`信息已处理，但卡片读取失败：${error.message}`, "warning"));
      markPending(item.id, false);
    }
  };
  const openPortal = async (item) => {
    try {
      await api(`/api/providers/${encodeURIComponent(item.recordId)}/portal`, {
        method: "POST",
        body: "{}",
      });
      notify(`已在默认浏览器打开 ${item.name}，会沿用浏览器现有登录状态`);
    } catch (error) {
      notify(error.message, "error");
    }
  };
  const selectRelayAccount = (account, source) => {
    if (!source || !account.selectedKeyId) {
      notify("该中转站账号还没有可用 Key，请先重新登录同步或创建 Key", "error");
      return;
    }
    select(source);
  };
  const updateRelaySelection = async (account, changes) => {
    const changingRemoteGroup = Boolean(changes.groupId);
    if (changingRemoteGroup && !account.dashboardSessionStored) {
      notify("修改中转站路由分组需要网页登录凭据，请重新登录一次", "warning");
      setRelayRelogin(account);
      return;
    }
    const pendingId = `relay:${account.id}`;
    markPending(pendingId, true);
    try {
      const selectedKeyId = changes.keyId || account.selectedKeyId;
      const result = await api(
        changingRemoteGroup
          ? `/api/relay-accounts/${encodeURIComponent(account.id)}/keys/${encodeURIComponent(selectedKeyId)}/group`
          : `/api/relay-accounts/${encodeURIComponent(account.id)}/selection`,
        {
          method: "POST",
          body: JSON.stringify(
            changingRemoteGroup
              ? { groupId: changes.groupId }
              : {
                  keyId: selectedKeyId,
                  endpointId: changes.endpointId || account.selectedEndpointId,
                },
          ),
        },
      );
      await reloadConnections();
      const key = result.result?.account?.keys?.find(
        (item) => String(item.id) === String(result.result.account.selectedKeyId),
      );
      const group = result.result?.account?.groups?.find(
        (item) => String(item.id) === String(key?.groupId),
      );
      notify(
        changingRemoteGroup
          ? `${account.siteName} 已在中转站网站把 ${key?.name || "当前 API Key"} 切换到 ${group?.name || "新分组"}，无需重启 Codex`
          : `${account.siteName} 已选择 ${key?.name || "新 API Key"}${result.result?.requiresReapply ? "；若 Codex 正在使用此账号，请点击右上角重新应用" : ""}`,
        result.result?.requiresReapply ? "warning" : "success",
      );
    } catch (error) {
      notify(error.message, "error");
    } finally {
      markPending(pendingId, false);
    }
  };
  const refreshRelayAccount = async (account) => {
    const pendingId = `relay:${account.id}`;
    markPending(pendingId, true);
    try {
      const result = await api(`/api/relay-accounts/${encodeURIComponent(account.id)}/refresh`, {
        method: "POST",
        body: "{}",
        timeoutMs: 120_000,
      });
      await reloadConnections();
      if (result.result?.requiresLogin || result.result?.requiresBrowser) {
        if (result.result?.originMismatch) notify("网站地址与保存记录不同，请按提示重新连接", "warning");
        else if (result.result?.requiresBrowser) notify("站点要求通过网页验证，正在打开网页登录窗口；验证完成后会同步余额", "warning");
        setRelayRelogin({...(result.result?.account || account), browserRefresh: Boolean(result.result?.requiresBrowser), originMismatch: result.result?.originMismatch});
        return;
      }
      const warnings = result.result?.warnings || [];
      const removed = Number(result.result?.removedKeyCount) || 0;
      notify(warnings.length ? `${account.siteName} 已刷新；${warnings[0]}` : `${account.siteName} 的 Key、分组、余额与模型已同步${removed ? `；移除 ${removed} 个网站已删除的 Key` : ""}`, warnings.length ? "warning" : "success");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      markPending(pendingId, false);
    }
  };
  const toggleRelayProxy = (_account, source, enabled) => {
    if (!source) return;
    toggleProxy(source, enabled);
  };
  const moveRelayAccountGroup = async (account, source, nextGroupId) => {
    if (String(nextGroupId) === String(account.groupId || "")) return;
    const previousGroupId = String(account.groupId || source?.groupId || "");
    const pendingId = `relay:${account.id}`;
    markPending(pendingId, true);
    patchRelayAccountGroup(account, source, nextGroupId);
    try {
      const response = await api(`/api/relay-accounts/${encodeURIComponent(account.id)}/group`, {
        method: "POST",
        body: JSON.stringify({ groupId: nextGroupId }),
      });
      patchRelayAccountGroup(account, source, nextGroupId, response.result);
      notify(
        `${account.name || account.siteName || "中转站账号"} 已移动到 ${groupMap[nextGroupId]?.name || "新分组"}`,
      );
    } catch (error) {
      patchRelayAccountGroup(account, source, previousGroupId);
      notify(error.message, "error");
    } finally {
      markPending(pendingId, false);
    }
  };
  const exportRelayAccount = async (account) => {
    const pendingId = `relay:${account.id}`;
    markPending(pendingId, true);
    try {
      const result = await api(
        `/api/relay-accounts/${encodeURIComponent(account.id)}/export`,
      );
      setExportModal({
        item: {
          id: pendingId,
          kind: "relay",
          recordId: account.id,
          name: account.name || account.siteName || "中转站账号",
        },
        payload: result.export,
      });
    } catch (error) {
      notify(error.message, "error");
    } finally {
      markPending(pendingId, false);
    }
  };
  const removeRelayAccount = async (account) => {
    const approved = await confirm({
      tone: "danger",
      title: `删除“${account.name || account.siteName || "中转站账号"}”？`,
      message: "整站账号快照、所有本机 DPAPI 加密 Key 和对应模型 Provider 都会一并删除。",
      detail: "删除前自动保存本机加密恢复点，可在设置中预览恢复。网站上的 Key 和账号不受影响。",
      confirmLabel: "删除本机账号",
    });
    if (!approved) return;
    act(async () => {
      await api(`/api/relay-accounts/${encodeURIComponent(account.id)}`, { method: "DELETE" });
      notify(`${account.name || account.siteName || "中转站账号"} 已从本机删除`);
    });
  };
  const toggleProxy = async (item, enabled) => {
    const poolKey = item.kind === "provider" ? "providerIds" : "accountIds";
    const previousPool = [...(data.settings.web2api?.[poolKey] || [])];
    const previousOrder = [...(data.settings.web2api?.sourceOrder || [])];
    const sourceId = `${item.kind}:${item.recordId}`;
    const nextPool = enabled
      ? [...previousPool.filter((id) => id !== item.recordId), item.recordId]
      : previousPool.filter((id) => id !== item.recordId);
    const nextOrder = enabled
      ? [...previousOrder.filter((id) => id !== sourceId), sourceId]
      : previousOrder.filter((id) => id !== sourceId);
    markPending(item.id, true);
    patchSource(item, { proxyEnabled: enabled });
    updateData((current) => ({
      ...current,
      settings: {
        ...current.settings,
        web2api: {
          ...current.settings.web2api,
          [poolKey]: nextPool,
          sourceOrder: nextOrder,
        },
      },
      web2apiStatus: { ...current.web2apiStatus, memberCount: nextPool.length },
    }));
    try {
      await api(
        `/api/${item.kind === "provider" ? "providers" : "accounts"}/${encodeURIComponent(item.recordId)}/proxy`,
        {
        method: "POST",
        body: JSON.stringify({ enabled }),
        },
      );
      notify(
        enabled
          ? `${item.name} 已加入本地 API 号池`
          : `${item.name} 已移出本地 API 号池`,
      );
    } catch (error) {
      patchSource(item, { proxyEnabled: !enabled });
      updateData((current) => ({
        ...current,
        settings: {
          ...current.settings,
          web2api: {
            ...current.settings.web2api,
            [poolKey]: previousPool,
            sourceOrder: previousOrder,
          },
        },
        web2apiStatus: {
          ...current.web2apiStatus,
          memberCount: previousPool.length,
        },
      }));
      notify(error.message, "error");
    } finally {
      markPending(item.id, false);
    }
  };
  const moveGroup = async (item, nextGroupId) => {
    if (nextGroupId === item.groupId) return;
    const previousGroupId = item.groupId;
    markPending(item.id, true);
    patchSource(item, { groupId: nextGroupId });
    try {
      await api(
        `/api/account-groups/${encodeURIComponent(nextGroupId)}/assign`,
        {
          method: "POST",
          body: JSON.stringify({
            accountIds: item.kind === "account" ? [item.recordId] : [],
            providerIds: item.kind === "provider" ? [item.recordId] : [],
          }),
        },
      );
      notify(
        `${item.name} 已移动到 ${groupMap[nextGroupId]?.name || "新分组"}`,
      );
    } catch (error) {
      patchSource(item, { groupId: previousGroupId });
      notify(error.message, "error");
    } finally {
      markPending(item.id, false);
    }
  };
  const exportOne = async (item) => {
    markPending(item.id, true);
    try {
      const path =
        item.kind === "account"
          ? `/api/accounts/${encodeURIComponent(item.recordId)}/export`
          : `/api/providers/${encodeURIComponent(item.recordId)}/export`;
      const result = await api(path);
      setExportModal({ item, payload: result.export });
    } catch (error) {
      notify(error.message, "error");
    } finally {
      markPending(item.id, false);
    }
  };
  const remove = async (item) => {
    const approved = await confirm({
      tone: "danger",
      title: `删除“${item.name}”？`,
      message: "账号卡片、本机加密凭据和相关模型路由都会一并删除。",
      detail: "删除前自动保存本机加密恢复点，可在设置中预览恢复；远端登录不受影响。",
      confirmLabel: "删除本机记录",
    });
    if (!approved) return;
    act(async () => {
      const path =
        item.kind === "account"
          ? `/api/accounts/${encodeURIComponent(item.recordId)}`
          : `/api/providers/${encodeURIComponent(item.recordId)}`;
      const result = await api(path, { method: "DELETE" });
      setSelectedIds((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
      notify(
        result.configurationWarning
          ? `${item.name} 已删除；${result.configurationWarning}`
          : `${item.name} 已删除，相关子代理槽位已恢复默认`,
        result.configurationWarning ? "warning" : "success",
      );
    });
  };
  const refreshAll = () =>
    act(async () => {
      const accountRefresh = () => api("/api/accounts/refresh", {
        method: "POST",
        body: "{}",
      });
      const relayProviderIds = new Set(relayAccounts.map(account => account.providerId));
      const relayAccountIds = new Set(relayAccounts.map(account => account.id));
      const needsLogin = [];
      const relayRefreshes = relayAccounts.map(account => async () => {
        const response = await api(`/api/relay-accounts/${encodeURIComponent(account.id)}/refresh`, { method:"POST",body:JSON.stringify({full:true}),timeoutMs:120_000 });
        if (response.result?.requiresLogin) needsLogin.push(response.result.account || account);
        return {...response,relayRefresh:true};
      });
      const providerRefreshes = data.settings.providers
        .filter((item) => item.kind === "custom" && item.keyConfigured && !relayAccountIds.has(item.relayAccountId) && !relayProviderIds.has(item.id))
        .map((item) => () => refreshProviderMetadata(item.id));
      const results = await runBoundedRefresh([accountRefresh, ...relayRefreshes, ...providerRefreshes]);
      const notice = batchRefreshNotice(results);
      notify(notice.message, notice.kind);
      if (needsLogin.length) setRelayRelogin(needsLogin[0]);
    });
  const deleteInvalid = async () => {
    if (!invalidCount) return notify("当前分组没有确认失效的账号");
    const approved = await confirm({
      tone: "danger",
      title: `删除当前分组的 ${invalidCount} 个失效账号？`,
      message: `仅会处理当前正在查看的“${groupId === "all" ? "所有账号" : groupMap[groupId]?.name || "分组"}”，其他分组不受影响。`,
      detail: "先自动创建本机恢复点，再清理加密凭据与关联路由。",
      confirmLabel: "删除失效账号",
    });
    if (!approved) return;
    act(async () => {
      const result = await api("/api/accounts/delete-invalid", {
        method: "POST",
        body: JSON.stringify({ groupId }),
      });
      notify(`已删除 ${result.result.deleted} 个失效账号`);
    });
  };
  const toggleSelected = (item) =>
    setSelectedIds((current) => {
      const next = new Set(current);
      next.has(item.id) ? next.delete(item.id) : next.add(item.id);
      return next;
    });
  const deleteSelected = async () => {
    if (!selectedItems.length) return;
    const approved = await confirm({
      tone: "danger",
      title: `删除已选择的 ${selectedItems.length} 项？`,
      message: "所选账号/中转站、本机加密凭据和引用它们的模型路由都会被清理。",
      detail: "删除前自动保存本机加密恢复点，可在设置中预览恢复。",
      confirmLabel: `删除 ${selectedItems.length} 项`,
    });
    if (!approved) return;
    act(async () => {
      const result = await api("/api/model-sources/delete-batch", {
        method: "POST",
        body: JSON.stringify({
          accountIds: selectedItems
            .filter((item) => item.kind === "account")
            .map((item) => item.recordId),
          providerIds: selectedItems
            .filter((item) => item.kind === "provider")
            .map((item) => item.recordId),
        }),
      });
      setSelectedIds(new Set());
      setSelectionMode(false);
      notify(
        result.result.configurationWarning
          ? `已删除 ${result.result.deleted} 个；${result.result.configurationWarning}`
          : result.result.failed.length
          ? `已删除 ${result.result.deleted} 个，${result.result.failed.length} 个未能删除`
          : `已批量删除 ${result.result.deleted} 个账号`,
        result.result.configurationWarning
          ? "warning"
          : result.result.failed.length
            ? "error"
            : "success",
      );
    });
  };
  const setSelectedProxy = async (enabled) => {
    if (!selectedChatgptAccounts.length && !selectedApiProviders.length)
      return notify(
        "已选择的项目中没有可加入 API 的 ChatGPT 账号或 API Provider",
        "error",
      );
    setBatchBusy(true);
    try {
      const ids = selectedChatgptAccounts.map((item) => item.recordId);
      const providerIds = selectedApiProviders.map((item) => item.recordId);
      const accountResult = ids.length
        ? await api("/api/accounts/proxy-batch", {
            method: "POST",
            body: JSON.stringify({ accountIds: ids, enabled }),
          })
        : null;
      const providerResult = providerIds.length
        ? await api("/api/providers/proxy-batch", {
            method: "POST",
            body: JSON.stringify({ providerIds, enabled }),
          })
        : null;
      const pool = accountResult?.result?.pool || data.settings.web2api?.accountIds || [];
      const providerPool =
        providerResult?.result?.providerPool || data.settings.web2api?.providerIds || [];
      const sourceOrder =
        providerResult?.result?.sourceOrder ||
        accountResult?.result?.sourceOrder ||
        data.settings.web2api?.sourceOrder ||
        [];
      updateData((current) => ({
        ...current,
        settings: {
          ...current.settings,
          accounts: current.settings.accounts.map((account) =>
            ids.includes(account.id)
              ? { ...account, proxyEnabled: enabled }
              : account,
          ),
          providers: current.settings.providers.map((provider) =>
            providerIds.includes(provider.id)
              ? { ...provider, proxyEnabled: enabled }
              : provider,
          ),
          web2api: {
            ...current.settings.web2api,
            accountIds: pool,
            providerIds: providerPool,
            sourceOrder,
          },
        },
        web2apiStatus: {
          ...current.web2apiStatus,
          memberCount: pool.length + providerPool.length,
        },
      }));
      notify(
        enabled
          ? `已将 ${ids.length + providerIds.length} 个成员加入 API 号池`
          : `已将 ${ids.length + providerIds.length} 个成员移出 API 号池`,
      );
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBatchBusy(false);
    }
  };
  const consumeReset = async (item) => {
    const approved = await confirm({
      tone: "warning",
      title: `使用 ${item.name} 的 1 张重置卡？`,
      message: "官方会立即尝试重置当前 Codex 使用额度，并消耗 1 张可用重置卡。",
      detail: "如果当前额度不符合重置条件，官方应返回“无需重置”且不会扣卡。",
      confirmLabel: "确认使用重置卡",
    });
    if (!approved) return false;
    markPending(item.id, true);
    try {
      const result = await api(
        `/api/accounts/${encodeURIComponent(item.recordId)}/reset-credit/consume`,
        { method: "POST", body: "{}" },
      );
      patchSource(item, result.result.account);
      notify(
        result.result.outcome === "alreadyRedeemed"
          ? "该次重置已在官方完成，额度信息已刷新"
          : "重置卡已使用，额度信息已刷新",
      );
      return true;
    } catch (error) {
      notify(error.message, "error");
      return false;
    } finally {
      markPending(item.id, false);
    }
  };
  const loadResetDetails = async (item) => {
    markPending(item.id, true);
    try {
      const result = await api(
        `/api/accounts/${encodeURIComponent(item.recordId)}/reset-credit/details`,
        { method: "POST", body: "{}" },
      );
      patchSource(item, result.account);
      const detailError = result.account?.refreshWarnings?.resetCredits || result.account?.refreshErrors?.resetCredits;
      if (detailError) notify(detailError, "warning");
      return !detailError;
    } catch (error) {
      notify(error.message, "error");
      return false;
    } finally {
      markPending(item.id, false);
    }
  };
  const toggleSelectionMode = () => {
    setSelectionMode((value) => !value);
    setSelectedIds(new Set());
  };

  return (
    <>
      <section className="page accounts-page">
        <div className="page-heading">
          <div>
            <span className="eyebrow">WORKSPACE / ACCOUNTS</span>
            <h1>账号工作台</h1>
            <p>
              所有账号、模型和接入线路，一处管理。
            </p>
          </div>
          <div className="heading-actions">
            <button className="button primary" onClick={() => setAddOpen(true)}>
              <UserPlus size={17} />
              添加账号
            </button>
            <button
              className="button secondary"
              onClick={refreshAll}
              disabled={busy}
            >
              {busy ? (
                <Loader2 className="spin" size={17} />
              ) : (
                <RefreshCw size={17} />
              )}
              全部刷新
            </button>
            <button
              className={cx(
                "button",
                selectionMode ? "danger-outline" : "secondary",
              )}
              onClick={toggleSelectionMode}
              disabled={busy}
            >
              <CheckSquare size={17} />
              {selectionMode ? "退出批量" : "批量操作"}
            </button>
            <button
              className="button danger-outline"
              onClick={deleteInvalid}
              disabled={busy || !invalidCount}
            >
              <Trash2 size={17} />
              清理失效账号{invalidCount ? ` (${invalidCount})` : ""}
            </button>
          </div>
        </div>
        {data.auth?.liveSelection?.configured && !data.auth.liveSelection.recognized && <div className="external-source-notice" role="status">
          <Info size={17} /><span>Codex 当前使用的配置未匹配到本机账号。已保留外部配置；选择下方账号后可明确切换。</span>
        </div>}
        {["queued", "running"].includes(data.accountRefreshStatus?.status) && (
          <div className="import-refresh-strip" role="status">
            <Loader2 className="spin" size={17} />
            <span>
              <strong>正在后台同步新导入账号</strong>
              <small>卡片会自动更新，不影响继续添加、分组或切换账号。</small>
            </span>
            <b>
              {data.accountRefreshStatus.completed}/
              {data.accountRefreshStatus.total}
            </b>
          </div>
        )}
        <div className="account-filterbar" role="search" aria-label="搜索和筛选账号">
          <label className="account-search"><Search size={18} /><span className="visually-hidden">搜索账号、邮箱、站点或模型</span><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索账号、邮箱、站点或模型…" />{query && <button type="button" aria-label="清空搜索" onClick={() => setQuery("")}><X size={16} /></button>}</label>
        </div>
        <div className="group-bar" role="tablist" aria-label="账号分组">
          <button
            className={groupId === "all" ? "active" : ""}
            onClick={() => setGroupId("all")}
            role="tab"
            aria-selected={groupId === "all"}
          >
            <Users size={16} />
            所有账号 <span>{standaloneSources.length + relayAccounts.length}</span>
          </button>
          {groups.map((group) => (
            <div className={cx("group-tab", groupId === group.id && "active")} key={group.id}>
              <button
                data-card-group-drop={group.id}
                className={groupId === group.id ? "active" : ""}
                onClick={() => setGroupId(group.id)}
                role="tab"
                aria-selected={groupId === group.id}
              >
                <i className={`tone-${group.color}`} />
                {group.name}
                <span>
                  {standaloneSources.filter((item) => item.groupId === group.id).length
                    + relayAccounts.filter((item) => item.groupId === group.id).length}
                </span>
              </button>
              {!group.system && (
                <IconButton
                  label={`管理 ${group.name} 分组`}
                  onClick={() => setGroupModal(group)}
                >
                  <Pencil size={14} />
                </IconButton>
              )}
            </div>
          ))}
          <button className="add-group" onClick={() => setGroupModal({})}>
            <Plus size={16} />
            新建分组
          </button>
        </div>
        {selectionMode && (
          <div
            className="bulk-toolbar"
            role="toolbar"
            aria-label="批量账号操作"
          >
            <div>
              <CheckSquare size={17} />
              <strong>已选择 {selectedItems.length} 个</strong>
              <span>
                其中 {selectedChatgptAccounts.length + selectedApiProviders.length} 个可用于 API
              </span>
            </div>
            <button
              className="button subtle"
              onClick={() =>
                setSelectedIds((current) => {
                  const next = new Set(current);
                  visibleIds.forEach(id => next.add(id));
                  return next;
                })
              }
            >
              选择当前结果 ({visibleIds.length})
            </button>
            <button
              className="button subtle"
              onClick={() => setSelectedIds(new Set())}
              disabled={!selectedItems.length}
            >
              清空
            </button>
            <button
              className="button api-batch"
              onClick={() => setSelectedProxy(true)}
              disabled={
                (!selectedChatgptAccounts.length && !selectedApiProviders.length) ||
                batchBusy
              }
            >
              {batchBusy ? (
                <Loader2 className="spin" size={16} />
              ) : (
                <Server size={16} />
              )}
              加入 API
            </button>
            <button
              className="button subtle"
              onClick={() => setSelectedProxy(false)}
              disabled={
                (!selectedChatgptAccounts.length && !selectedApiProviders.length) ||
                batchBusy
              }
            >
              <X size={16} />
              移出 API
            </button>
            <button
              className="button danger-outline"
              onClick={deleteSelected}
              disabled={!selectedItems.length || busy || batchBusy}
            >
              <Trash2 size={16} />
              删除已选 ({selectedItems.length})
            </button>
          </div>
        )}
        {groupId === "all" && (
          <ApiPoolCard data={data} onOpen={() => setPoolOpen(true)} />
        )}
        <span className="visually-hidden" role="status" aria-live="polite">{cardDrag.status}</span>
        <div className="account-grid" ref={accountGridRef} onKeyDown={event => {
          if (!event.altKey || !event.shiftKey || !["ArrowUp","ArrowDown"].includes(event.key)
            || event.target.closest("input,textarea,select,[role=combobox],[contenteditable=true]")) return;
          const card = event.target.closest("[data-card-id]");
          if (!card) return;
          event.preventDefault(); event.stopPropagation();
          cardDrag.moveBy(card.dataset.cardId, event.key === "ArrowUp" ? -1 : 1);
        }}>
          {renderedCardRows.map((card) => {
            if (card.kind === "relay") {
              const { account, source } = card.row;
              return (
                <RelayAccountCard
                  key={`relay-account:${account.id}`}
                  account={account}
                  source={source}
                  selectionMode={selectionMode}
                  selected={Boolean(source && selectedIds.has(source.id))}
                  onToggle={toggleSelected}
                  groups={groups}
                  onSelect={selectRelayAccount}
                  onSelection={updateRelaySelection}
                  onGroup={moveRelayAccountGroup}
                  onManage={setRelayManage}
                  onRefresh={refreshRelayAccount}
                  onExport={exportRelayAccount}
                  onDelete={removeRelayAccount}
                  onProxy={toggleRelayProxy}
                  onPortal={openPortal}
                  onModels={() => setModelInspector({kind:"relay",id:account.id})}
                  syncToApi={(data.settings.web2api?.providerIds || []).includes(account.providerId)}
                  busy={busy || pendingIds.has(`relay:${account.id}`)}
                />
              );
            }
            const { item } = card;
            return (
              <AccountCard
                key={item.id}
                item={item}
                active={item.active}
                groups={groups}
                selectionMode={selectionMode}
                selected={selectedIds.has(item.id)}
                onToggle={toggleSelected}
                onSelect={select}
                onGroup={moveGroup}
                onRefresh={refresh}
                onPortal={openPortal}
                onEdit={setEditModal}
                onModels={() => setModelInspector({kind:"source",id:item.id})}
                onReauthenticate={() => setReauthAccount(item.record)}
                onExport={exportOne}
                onDelete={remove}
                onProxy={toggleProxy}
                onLoadReset={loadResetDetails}
                onConsumeReset={consumeReset}
                busy={busy || pendingIds.has(item.id)}
              />
            );
          })}
          {!cardRows.length && query ? <div className="account-search-empty"><Search size={30} /><h3>没有匹配的账号</h3><p>尝试邮箱、站点或模型名称。</p><button className="button secondary" onClick={() => setQuery("")}>清空搜索</button></div> : !cardRows.length && <EmptyState group={groupId} />}
        </div>
      </section>
      {addOpen && (
        <AddAccountModal
          groups={groups}
          onClose={() => setAddOpen(false)}
          onDone={reload}
          notify={notify}
        />
      )}
      {modelInspector && <ModelListModal
        source={modelInspector.kind === "relay" ? sources.find(source => source.id === `provider:${relayAccounts.find(account => account.id === modelInspector.id)?.providerId}`) : sources.find(source => source.id === modelInspector.id)}
        account={modelInspector.kind === "relay" ? relayAccounts.find(account => account.id === modelInspector.id) : null}
        notify={notify} onSaved={reloadConnections} onClose={() => setModelInspector(null)}
      />}
      {groupModal && (
        <GroupModal
          group={groupModal.id ? groupModal : null}
          confirm={confirm}
          onClose={() => setGroupModal(null)}
          onDone={async (result = {}) => {
            if (result.deletedGroupId === groupId) setGroupId("all");
            await reloadConnections().catch(error => notify(`分组修改已保存，列表刷新失败：${error.message}`, "warning"));
          }}
          notify={notify}
        />
      )}
      {poolOpen && (
        <ApiPoolModal
          data={data}
          updateData={updateData}
          notify={notify}
          confirm={confirm}
          onClose={() => setPoolOpen(false)}
        />
      )}
      {exportModal && (
        <AccountExportModal
          item={exportModal.item}
          payload={exportModal.payload}
          notify={notify}
          onClose={() => setExportModal(null)}
        />
      )}
      {editModal && (
        <AccountEditModal
          item={editModal}
          groups={groups}
          notify={notify}
          onDone={reload}
          onClose={() => setEditModal(null)}
          onReauthenticate={() => setReauthAccount(editModal.record)}
        />
      )}
      {reauthAccount && <AccountReauthModal account={reauthAccount} api={api} notify={notify} onDone={reloadConnections} onClose={() => setReauthAccount(null)} />}
      {relayManage && (
        <RelayAccountManageModal
          account={relayAccounts.find((item) => item.id === relayManage.id) || relayManage}
          notify={notify}
          onDone={reloadConnections}
          onRelogin={setRelayRelogin}
          onClose={() => setRelayManage(null)}
        />
      )}
      {relayRelogin && (
        <RelayReloginModal
          browserRefresh={Boolean(relayRelogin.browserRefresh)}
          originMismatch={relayRelogin.originMismatch}
          key={relayRelogin.id}
          account={relayAccounts.find((item) => item.id === relayRelogin.id) || relayRelogin}
          groupId={relayRelogin.groupId || groups[0]?.id || "relay"}
          syncToApi={(data.settings.web2api?.providerIds || []).includes(relayRelogin.providerId)}
          notify={notify}
          onDone={reloadConnections}
          onClose={() => setRelayRelogin(null)}
        />
      )}
      {switchProgress && (
        <SwitchProgressModal
          operation={switchProgress}
          onClose={() => setSwitchProgress(null)}
          retrying={busy}
          onRetry={() => {
            const target = sources.find(
              (item) =>
                item.kind === switchProgress.targetKind &&
                item.recordId === switchProgress.targetId,
            );
            if (!target) {
              notify("原账号或中转站已不存在，无法重试。", "error");
              setSwitchProgress(null);
              return;
            }
            void select(target);
          }}
        />
      )}
    </>
  );
}

function ModelSourcePicker({
  sources,
  selected,
  setSelected,
  defaultKey,
  setDefaultKey,
  aggregate,
}) {
  const [expandedGroups, setExpandedGroups] = useState(() => new Set());
  const [expandedSources, setExpandedSources] = useState(() => new Set());
  useEffect(() => {
    setExpandedGroups(new Set());
    setExpandedSources(new Set());
  }, [aggregate]);
  const visible = aggregate ? sources : sources.filter((item) => item.active);
  const toggleSource = (source) => {
    if (!source.available || !source.models.length) return;
    const keys = source.models.map((item) => item.key);
    const all = keys.length > 0 && keys.every((key) => selected.has(key));
    setSelected((current) => {
      const next = new Set(current);
      keys.forEach((key) => (all ? next.delete(key) : next.add(key)));
      return next;
    });
  };
  const toggleModel = (key) =>
    setSelected((current) => {
      const next = new Set(current);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  const toggleSet = (setter, id) =>
    setter((current) => {
      const next = new Set(current);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  const sourceCard = (source) => {
    const isOpen = expandedSources.has(source.id);
    const selectedCount = source.models.filter((model) =>
      selected.has(model.key),
    ).length;
    const statusText = source.invalidReason
      ? source.invalidReason
      : !source.models.length
        ? "模型目录尚未加载，可在账号页刷新"
        : source.modelCatalogSource === "local_runtime"
          ? `${source.models.length} 个模型 · 暂用本机 Codex 目录`
          : source.modelsError || source.refreshState === "error"
            ? `${source.models.length} 个模型 · 远端刷新失败，保留缓存`
            : `${source.models.length} 个可用模型`;
    return (
      <section
        className={cx(
          "model-source",
          source.active && "current",
          !source.available && "unavailable",
        )}
        key={source.id}
      >
        <header>
          <button
            type="button"
            className="expand-source"
            onClick={() => toggleSet(setExpandedSources, source.id)}
            aria-expanded={isOpen}
            aria-controls={`model-source-content-${source.id}`}
          >
            {isOpen ? <ChevronDown size={17} /> : <ChevronRight size={17} />}
            <span className={cx("source-symbol", source.kind)}>
              {source.kind === "provider" ? (
                <Server size={17} />
              ) : (
                <Globe2 size={17} />
              )}
            </span>
            <span>
              <strong>{source.name}</strong>
              <small>
                {statusText}
                {source.active ? " · 当前账号" : ""}
              </small>
              {source.kind === "account" && (
                <small title={source.modelsError || ""}>
                  {source.modelsSource === "chatgpt_account" ? "账号实时目录" : source.modelsSource === "codex_app_server" ? "Codex 运行时" : "本机缓存"}
                  {source.modelsRefreshedAt ? ` · ${formatDateTime(source.modelsRefreshedAt)}` : " · 尚未同步"}
                  {source.modelsStale ? " · 待刷新" : ""}
                </small>
              )}
            </span>
          </button>
          <button
            type="button"
            className={cx(
              "select-source-models",
              selectedCount === source.models.length &&
                source.models.length &&
                "checked",
            )}
            onClick={() => toggleSource(source)}
            disabled={!source.available || !source.models.length}
            title={
              source.available
                ? "选择该账号的全部模型"
                : source.invalidReason || source.refreshError || "该模型源暂不可用"
            }
          >
            <span>
              {selectedCount === source.models.length &&
              source.models.length ? (
                <Check size={13} />
              ) : (
                selectedCount || ""
              )}
            </span>
            {selectedCount}/{source.models.length}
          </button>
        </header>
        {isOpen && (
          <div className="model-check-list" id={`model-source-content-${source.id}`}>
            {!source.models.length && (
              <p className="model-source-empty">
                账号已保留在调度中心。请先在“选择账号”页刷新，或检查 Codex
                运行时；网络恢复后模型会自动补齐。
              </p>
            )}
            {source.models.map((model) => (
              <div
                className={cx(
                  "model-check",
                  selected.has(model.key) && "selected",
                )}
                key={model.key}
              >
                <button
                  onClick={() => toggleModel(model.key)}
                  disabled={!source.available}
                >
                  <span>{selected.has(model.key) && <Check size={13} />}</span>
                  <strong>{model.name}</strong>
                </button>
                <button
                  className={cx(
                    "default-model",
                    defaultKey === model.key && "active",
                  )}
                  title="设为默认模型"
                  onClick={() => {
                    setSelected(current => new Set([...current, model.key]));
                    setDefaultKey(model.key);
                  }}
                  disabled={!source.available}
                >
                  <Zap size={14} />
                  {defaultKey === model.key ? "默认" : "设为默认"}
                </button>
              </div>
            ))}
          </div>
        )}
      </section>
    );
  };
  if (!visible.length)
    return (
      <div className="inline-empty">
        <AlertTriangle size={19} />
        <span>请先回到“选择账号”页，选择一个可用账号。</span>
      </div>
    );
  if (!aggregate)
    return <div className="source-picker">{visible.map(sourceCard)}</div>;
  return (
    <div className="source-picker grouped-source-picker">
      {groupedSources(visible).map((group) => {
        const open = expandedGroups.has(group.id);
        const groupModels = group.sources
          .filter((source) => source.available)
          .flatMap((source) => source.models);
        const allGroupModelCount = group.sources.reduce(
          (count, source) => count + source.models.length,
          0,
        );
        const selectedCount = groupModels.filter((model) =>
          selected.has(model.key),
        ).length;
        return (
          <section className="model-source-group" key={group.id}>
            <header>
              <button
                type="button"
                className="expand-source-group"
                onClick={() => toggleSet(setExpandedGroups, group.id)}
                aria-expanded={open}
                aria-controls={`model-source-group-content-${group.id}`}
              >
                {open ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
                <span>
                  <Layers3 size={17} />
                </span>
                <span>
                  <strong>{group.name}</strong>
                  <small>
                    {group.sources.length} 个账号 · {allGroupModelCount} 个模型
                  </small>
                </span>
              </button>
              <button
                type="button"
                className="group-selection-count"
                onClick={() => {
                  const all =
                    groupModels.length > 0 &&
                    groupModels.every((model) => selected.has(model.key));
                  setSelected((current) => {
                    const next = new Set(current);
                    groupModels.forEach((model) =>
                      all ? next.delete(model.key) : next.add(model.key),
                    );
                    return next;
                  });
                }}
                disabled={!groupModels.length}
              >
                {selectedCount}/{groupModels.length}
              </button>
            </header>
            {open && (
              <div className="source-group-body" id={`model-source-group-content-${group.id}`}>
                {group.sources.map(sourceCard)}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}

function HierarchicalModelPicker({
  value,
  onChange,
  sources,
  usedKeys,
  slot,
  onClose,
}) {
  const [expandedGroups, setExpandedGroups] = useState(() => new Set());
  const [expandedSources, setExpandedSources] = useState(() => new Set());
  const toggle = (setter, id) =>
    setter((current) => {
      const next = new Set(current);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  return (
    <Modal
      title={slot === 0 ? "选择首选模型" : `选择备用 ${slot} 模型`}
      description="按分组 → 账号 → 模型逐级展开，避免一次显示过多选项"
      onClose={onClose}
      wide
    >
      {value && (
        <div className="route-current">
          <span>当前选择</span>
          <strong>{breadcrumbForModel(sources, value)}</strong>
        </div>
      )}
      <div className="hierarchy-picker">
        {slot > 0 && (
          <button
            className={cx("picker-unset", !value && "selected")}
            onClick={() => {
              onChange("");
              onClose();
            }}
          >
            <span>{!value && <Check size={13} />}</span>
            <strong>不设置</strong>
            <small>清空这个备用槽位，不参与回退</small>
          </button>
        )}
        {groupedSources(sources).map((group) => {
          const groupOpen = expandedGroups.has(group.id);
          const modelCount = group.sources.reduce(
            (sum, source) => sum + source.models.length,
            0,
          );
          return (
            <section className="picker-group" key={group.id}>
              <button
                className="picker-group-row"
                onClick={() => toggle(setExpandedGroups, group.id)}
              >
                {groupOpen ? (
                  <ChevronDown size={18} />
                ) : (
                  <ChevronRight size={18} />
                )}
                <span className="picker-group-icon">
                  <Layers3 size={17} />
                </span>
                <span>
                  <strong>{group.name}</strong>
                  <small>
                    {group.sources.length} 个账号 · {modelCount} 个模型
                  </small>
                </span>
              </button>
              {groupOpen && (
                <div className="picker-source-list">
                  {group.sources.map((source) => {
                    const sourceOpen = expandedSources.has(source.id);
                    return (
                      <section className="picker-source" key={source.id}>
                        <button
                          className="picker-source-row"
                          onClick={() => toggle(setExpandedSources, source.id)}
                        >
                          {sourceOpen ? (
                            <ChevronDown size={17} />
                          ) : (
                            <ChevronRight size={17} />
                          )}
                          <span className={cx("source-symbol", source.kind)}>
                            {source.kind === "provider" ? (
                              <Server size={16} />
                            ) : (
                              <Globe2 size={16} />
                            )}
                          </span>
                          <span>
                            <strong>{source.name}</strong>
                            <small>{source.models.length} 个可用模型</small>
                          </span>
                        </button>
                        {sourceOpen && (
                          <div className="picker-model-list">
                            {source.models.map((model) => {
                              const disabled =
                                usedKeys.has(model.key) && model.key !== value;
                              return (
                                <button
                                  className={cx(
                                    "picker-model",
                                    value === model.key && "selected",
                                  )}
                                  disabled={disabled}
                                  key={model.key}
                                  onClick={() => {
                                    onChange(model.key);
                                    onClose();
                                  }}
                                >
                                  <span>
                                    {value === model.key && <Check size={13} />}
                                  </span>
                                  <strong>{model.name}</strong>
                                  {disabled && <small>本级已选择</small>}
                                </button>
                              );
                            })}
                          </div>
                        )}
                      </section>
                    );
                  })}
                </div>
              )}
            </section>
          );
        })}
      </div>
      <div className="modal-footer picker-footer">
        <span>{slot === 0 ? "首选模型建议始终设置" : "备用模型可留空"}</span>
        {value && (
          <button
            className="button secondary"
            onClick={() => {
              onChange("");
              onClose();
            }}
          >
            清除此槽位
          </button>
        )}
        <button className="button primary" onClick={onClose}>
          完成
        </button>
      </div>
    </Modal>
  );
}

function RouteSelect({
  value,
  effort,
  onChange,
  onEffortChange,
  sources,
  slot,
  usedKeys,
  allEfforts,
}) {
  const [open, setOpen] = useState(false);
  const breadcrumb = value ? breadcrumbForModel(sources, value) : "";
  const selectedModel = sources
    .flatMap((source) => source.models)
    .find((model) => model.key === value);
  const supportedEfforts = selectedModel?.reasoningKnown
    ? selectedModel.efforts || []
    : allEfforts;
  const defaultEffort = selectedModel?.defaultEffort || "";
  return (
    <div className="route-select">
      <div className="route-select-heading">
        <span>{slot === 0 ? "首选" : `备用 ${slot}`}</span>
        <small>{slot === 0 ? "优先尝试" : `第 ${slot} 级回退`}</small>
      </div>
      <button
        className={cx("route-picker-trigger", value && "selected")}
        onClick={() => setOpen(true)}
        title={breadcrumb || (slot === 0 ? "选择模型" : "不设置")}
      >
        <span className="route-model-copy">{selectedModel ? <><strong>{selectedModel.label || selectedModel.id}</strong><small>{sources.find(source => source.models.some(model => model.key === value))?.name}</small></> : breadcrumb || (slot === 0 ? "选择模型" : "不设置")}</span>
        <ChevronDown size={15} />
      </button>
      <label className={cx("route-effort", !value && "disabled")}>
        <span>思考程度</span>
        <select
          value={effort || ""}
          disabled={!value}
          aria-label={`${slot === 0 ? "首选" : `备用 ${slot}`}模型思考程度`}
          title={defaultEffort ? `模型默认：${effortLabels[defaultEffort] || defaultEffort}` : "自动使用模型默认思考程度"}
          onChange={(event) => onEffortChange(event.target.value)}
        >
          <option value="">
            自动
          </option>
          {supportedEfforts.map((item) => (
            <option value={item} key={item}>
              {effortLabels[item] || item}
            </option>
          ))}
        </select>
      </label>
      {open && (
        <HierarchicalModelPicker
          value={value}
          onChange={onChange}
          sources={sources}
          slot={slot}
          usedKeys={usedKeys}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}

const runtimeTuningDefaults = {
  configManaged: false,
  managedFields: [],
  planningManaged: false,
  enabled: false,
  modelContextWindow: 0,
  autoCompactTokenLimit: 0,
  autoCompactScope: "total",
  mcpOptionalStartupGraceMs: -1,
  webSearch: "",
  webSearchContextSize: "",
  serviceTier: "",
  vpnCompatibility: false,
  preventIdleSleep: false,
  planningMode: "auto",
};

function configEntryId(entry) {
  return JSON.stringify(entry.path || []);
}

function configSectionLabel(section) {
  return {
    root: "根级配置",
    model_providers: "模型服务",
    tui: "终端界面",
    history: "历史记录",
    features: "功能开关",
    agents: "代理配置",
    mcp_servers: "MCP 服务",
    profiles: "配置档案",
    projects: "项目信任",
    sandbox_workspace_write: "工作区沙箱",
    shell_environment_policy: "终端环境",
    tools: "工具配置",
    windows: "Windows 配置",
    otel: "遥测配置",
  }[section] || section;
}

function configEntryDraft(entry) {
  if (entry.type === "array") return JSON.stringify(entry.value, null, 2);
  if (entry.type === "boolean") return Boolean(entry.value);
  return String(entry.value ?? "");
}

function parseConfigEntryDraft(entry, draft) {
  if (entry.type === "boolean") return Boolean(draft);
  if (entry.type === "integer") {
    const text = String(draft).trim();
    if (!/^-?\d+$/.test(text) || !Number.isSafeInteger(Number(text)))
      throw new Error(`配置字段 ${entry.key} 必须是有效整数`);
    return Number(text);
  }
  if (entry.type === "float") {
    const value = Number(String(draft).trim());
    if (!Number.isFinite(value)) throw new Error(`配置字段 ${entry.key} 必须是有效数字`);
    return value;
  }
  if (entry.type === "array") {
    let value;
    try {
      value = JSON.parse(String(draft));
    } catch {
      throw new Error(`配置字段 ${entry.key} 的数组 JSON 无效`);
    }
    if (!Array.isArray(value)) throw new Error(`配置字段 ${entry.key} 必须是数组`);
    return value;
  }
  return String(draft);
}

function ConfigEntryEditor({ entry, value, onChange }) {
  const id = configEntryId(entry);
  if (!entry.editable) {
    return (
      <pre className="config-entry-readonly">
        {typeof entry.value === "string" ? entry.value : JSON.stringify(entry.value, null, 2)}
      </pre>
    );
  }
  if (entry.type === "boolean") {
    return (
      <Switch
        checked={Boolean(value)}
        onChange={(checked) => onChange(id, checked)}
        label={value ? "开启" : "关闭"}
        ariaLabel={`修改 ${entry.key}`}
      />
    );
  }
  if (entry.type === "array" || String(value).includes("\n") || String(value).length > 120) {
    return (
      <textarea
        className="config-entry-array"
        value={value}
        aria-label={`修改 ${entry.key}`}
        onChange={(event) => onChange(id, event.target.value)}
      />
    );
  }
  return (
    <input
      type={entry.type === "integer" || entry.type === "float" ? "number" : "text"}
      step={entry.type === "float" ? "any" : undefined}
      value={value}
      aria-label={`修改 ${entry.key}`}
      onChange={(event) => onChange(id, event.target.value)}
    />
  );
}

function normalizeRuntimeTuning(value) {
  return { ...runtimeTuningDefaults, ...(value || {}) };
}

function codexVersionAtLeast(value, expectedMajor, expectedMinor, expectedPatch = 0) {
  const match = String(value || "").match(/(?:^|\D)(\d+)\.(\d+)\.(\d+)/);
  if (!match) return false;
  const actual = match.slice(1).map(Number);
  const expected = [expectedMajor, expectedMinor, expectedPatch];
  for (let index = 0; index < expected.length; index += 1) {
    if (actual[index] !== expected[index]) return actual[index] > expected[index];
  }
  return true;
}

function OrchestrationView({
  data,
  reload,
  notify,
  confirm,
  setBusy,
  busy,
  onDraftStateChange,
  sessionHistory,
  onSessionHistoryChange,
}) {
  const workspace = data.settings.modelWorkspace;
  const [mode, setMode] = useState(workspace.mode);
  const [selected, setSelected] = useState(
    () => new Set(data.selectedModelKeys),
  );
  const [defaultKey, setDefaultKey] = useState(
    workspace.defaultModelKey || data.selectedModelKeys[0] || "",
  );
  const [routing, setRouting] = useState(data.effectiveSubagentRouting);
  const [runtimeTuning, setRuntimeTuning] = useState(() =>
    normalizeRuntimeTuning(data.settings.runtimeTuning),
  );
  const [configDocument, setConfigDocument] = useState(null);
  const [configRaw, setConfigRaw] = useState("");
  const [configEntryDrafts, setConfigEntryDrafts] = useState({});
  const [configView, setConfigView] = useState("raw");
  const [configLoading, setConfigLoading] = useState(true);
  const [configSaving, setConfigSaving] = useState(false);
  const [configError, setConfigError] = useState("");
  // Expansion is presentation state, not routing configuration.  Always start
  // collapsed so entering the page never exposes or dirties advanced routes.
  const [advanced, setAdvanced] = useState(false);
  const [runtimeStatus, setRuntimeStatus] = useState(null);
  const [runtimeRepairing, setRuntimeRepairing] = useState(false);
  const [modelsRefreshing, setModelsRefreshing] = useState(false);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const initialDraftRef = useRef(
    JSON.stringify({
      mode: workspace.mode,
      selected: [...data.selectedModelKeys].sort(),
      defaultKey: workspace.defaultModelKey || data.selectedModelKeys[0] || "",
      routing: data.effectiveSubagentRouting,
      runtimeTuning: normalizeRuntimeTuning(data.settings.runtimeTuning),
    }),
  );
  const runtimeTouchedRef = useRef(false);
  const configInitialRawRef = useRef("");
  const configInitialDraftsRef = useRef({});
  const hydrateCodexConfigDocument = useCallback((document, syncRuntime = false) => {
    const nextDocument = document || {
      valid: true,
      content: "",
      entries: [],
      common: {},
    };
    const drafts = Object.fromEntries(
      (nextDocument.entries || []).map((entry) => [configEntryId(entry), configEntryDraft(entry)]),
    );
    setConfigDocument(nextDocument);
    setConfigRaw(nextDocument.content || "");
    setConfigEntryDrafts(drafts);
    setConfigError(nextDocument.error || "");
    configInitialRawRef.current = nextDocument.content || "";
    configInitialDraftsRef.current = drafts;
    if (!nextDocument.valid) setConfigView("raw");
    if (syncRuntime && !runtimeTouchedRef.current) {
      setRuntimeTuning((current) => {
        const common = nextDocument.common || {};
        const managed = new Set(current.managedFields || []);
        const fromConfig = (field, value, fallback) =>
          managed.has(field) ? fallback : (value ?? fallback);
        const next = normalizeRuntimeTuning({
          ...current,
          modelContextWindow: fromConfig(
            "modelContextWindow", common.modelContextWindow, current.modelContextWindow,
          ),
          autoCompactTokenLimit: fromConfig(
            "autoCompactTokenLimit", common.autoCompactTokenLimit, current.autoCompactTokenLimit,
          ),
          autoCompactScope: fromConfig(
            "autoCompactScope", common.autoCompactScope, current.autoCompactScope,
          ),
          mcpOptionalStartupGraceMs: fromConfig(
            "mcpOptionalStartupGraceMs",
            common.mcpOptionalStartupGraceMs,
            current.mcpOptionalStartupGraceMs,
          ),
          webSearch: fromConfig("webSearch", common.webSearch, current.webSearch),
          webSearchContextSize: fromConfig(
            "webSearchContextSize", common.webSearchContextSize, current.webSearchContextSize,
          ),
          serviceTier: fromConfig("serviceTier", common.serviceTier, current.serviceTier),
          vpnCompatibility: fromConfig(
            "vpnCompatibility", common.vpnCompatibility, current.vpnCompatibility,
          ),
          preventIdleSleep: fromConfig(
            "preventIdleSleep", common.preventIdleSleep, current.preventIdleSleep,
          ),
        });
        const initial = JSON.parse(initialDraftRef.current);
        initial.runtimeTuning = next;
        initialDraftRef.current = JSON.stringify(initial);
        return next;
      });
    }
  }, []);
  const loadCodexConfig = useCallback(async (syncRuntime = false) => {
    setConfigLoading(true);
    setConfigError("");
    try {
      const result = await api("/api/codex-config");
      hydrateCodexConfigDocument(result.document, syncRuntime);
      return result.document;
    } catch (error) {
      setConfigError(error.message);
      throw error;
    } finally {
      setConfigLoading(false);
    }
  }, [hydrateCodexConfigDocument]);
  useEffect(() => {
    loadCodexConfig(true).catch(() => {});
  }, [loadCodexConfig]);
  useEffect(() => {
    if (runtimeStatusResourceCache.loaded) {
      setRuntimeStatus(runtimeStatusResourceCache.value);
      return;
    }
    api("/api/codex-runtime")
      .then((result) => {
        runtimeStatusResourceCache = { loaded: true, value: result.runtime || null };
        setRuntimeStatus(result.runtime);
      })
      .catch(() => {
        runtimeStatusResourceCache = { loaded: true, value: null };
        setRuntimeStatus(null);
      });
  }, []);
  const repairRuntime = async () => {
    setRuntimeRepairing(true);
    try {
      const result = await api("/api/codex-runtime/deploy", {
        method: "POST",
        body: "{}",
        timeoutMs: 600_000,
      });
      runtimeStatusResourceCache = { loaded: true, value: result.runtime || result.status || null };
      setRuntimeStatus(runtimeStatusResourceCache.value);
      notify(result.message || "Codex 原生运行时已自动部署并通过检查");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setRuntimeRepairing(false);
    }
  };
  // Editor values are intentionally initialized only on mount. Background
  // account work is polled through its bounded status effect and must never
  // replace an unsaved orchestration draft.
  const usableSources = data.modelSources.filter(
    (source) => source.available && source.models.length,
  );
  const visibleKeys = usableSources
    .filter((source) => mode === "aggregate" || source.active)
    .flatMap((source) => source.models.map((model) => model.key));
  const visibleSelectedCount = visibleKeys.filter((key) =>
    selected.has(key),
  ).length;
  const parentSourceId = data.modelSources.find(source => source.active)?.id || workspace.activeSourceId;
  const crossSourceSubagents = Object.values(routing.routes || {}).some(route =>
    (route.models || []).some(key => String(key).split("::", 1)[0] !== parentSourceId));
  const sharedSubagentGateway = mode === "aggregate" || data.settings.web2api?.activeForCodex || crossSourceSubagents;
  const draftSnapshot = JSON.stringify({
    mode,
    selected: [...selected].sort(),
    defaultKey,
    routing,
    runtimeTuning,
  });
  const orchestrationDirty = draftSnapshot !== initialDraftRef.current;
  useEffect(() => {
    // Entering this view also starts a state read. Hydrate its response while
    // pristine; a late response must never replace edits already made here.
    if (draftSnapshot !== initialDraftRef.current) return;
    const nextDefault = data.settings.modelWorkspace.defaultModelKey || data.selectedModelKeys[0] || "";
    setMode(data.settings.modelWorkspace.mode);
    setSelected(new Set(data.selectedModelKeys));
    setDefaultKey(nextDefault);
    setRouting(data.effectiveSubagentRouting);
    initialDraftRef.current = JSON.stringify({
      mode: data.settings.modelWorkspace.mode,
      selected: [...data.selectedModelKeys].sort(),
      defaultKey: nextDefault,
      routing: data.effectiveSubagentRouting,
      runtimeTuning,
    });
  }, [data.modelSources, data.selectedModelKeys, data.effectiveSubagentRouting]);
  const refreshModels = async () => {
    if (modelsRefreshing || busy) return;
    setModelsRefreshing(true);
    setBusy(true);
    const previousSources = data.modelSources;
    try {
      const targets = previousSources.filter(source =>
        (mode === "aggregate" || source.active) &&
        (source.kind === "provider" || source.authMode === "chatgpt"));
      if (!targets.length) {
        notify("请先选择可刷新的账号或中转站", "warning");
        return;
      }
      const results = await runBoundedRefresh(targets.map(source => () => api(
        source.kind === "account"
          ? `/api/accounts/${encodeURIComponent(source.recordId)}/refresh`
          : `/api/providers/${encodeURIComponent(source.recordId)}/models`,
        { method: "POST", body: "{}" },
      )));
      const fresh = await reload();
      setSelected(current => reconcileModelSelection(current, previousSources, fresh.modelSources));
      const availableKeys = new Set(fresh.modelSources.filter(source => source.available).flatMap(source => source.models.map(model => model.key)));
      setDefaultKey(current => availableKeys.has(current) ? current : fresh.selectedModelKeys[0] || "");
      const notice = batchRefreshNotice(results);
      notify(notice.kind === "success" ? "模型目录已更新；全选账号会自动纳入新增模型" : notice.message, notice.kind);
      return fresh;
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setModelsRefreshing(false);
      setBusy(false);
    }
  };
  const configRawDirty = Boolean(configDocument) && configRaw !== configInitialRawRef.current;
  const configVisualDirty = Boolean(configDocument) && Object.keys(configEntryDrafts).some(
    (key) => configEntryDrafts[key] !== configInitialDraftsRef.current[key],
  );
  const configDirty = configRawDirty || configVisualDirty;
  const dirty = orchestrationDirty || configDirty;
  const configSections = useMemo(() => {
    const sections = new Map();
    for (const entry of configDocument?.entries || []) {
      const key = entry.section || "root";
      if (!sections.has(key)) sections.set(key, []);
      sections.get(key).push(entry);
    }
    return [...sections.entries()];
  }, [configDocument]);
  const selectVisible = () =>
    setSelected((current) => {
      const next = new Set(current);
      visibleKeys.forEach((key) => next.add(key));
      return next;
    });
  const clearVisible = () =>
    setSelected((current) => {
      const next = new Set(current);
      visibleKeys.forEach((key) => next.delete(key));
      return next;
    });
  const updateRoute = (level, slot, key) =>
    setRouting((current) => {
      const models = [...(current.routes[level]?.models || [])];
      const efforts = [...(current.routes[level]?.efforts || [])];
      models[slot] = key;
      efforts[slot] = "";
      const candidates = models
        .map((model, index) => ({ model, effort: efforts[index] || "" }))
        .filter((candidate) => candidate.model)
        .slice(0, 3);
      return {
        ...current,
        routes: {
          ...current.routes,
          [level]: {
            models: candidates.map((candidate) => candidate.model),
            efforts: candidates.map((candidate) => candidate.effort),
          },
        },
      };
    });
  const updateRouteEffort = (level, slot, effort) =>
    setRouting((current) => {
      const models = [...(current.routes[level]?.models || [])];
      const efforts = [...(current.routes[level]?.efforts || [])];
      if (!models[slot]) return current;
      efforts[slot] = effort;
      return {
        ...current,
        routes: {
          ...current.routes,
          [level]: {
            models,
            efforts: models.map((_, index) => efforts[index] || ""),
          },
        },
      };
    });
  const chooseStrategy = (strategy) => {
    if (strategy.id === "verification_first") setAdvanced(false);
    setRouting((current) => ({
      ...current,
      strategyId: strategy.id,
      prompt: strategy.instructions,
    }));
  };
  const updateRuntimeTuning = (key, value) => {
    runtimeTouchedRef.current = true;
    setRuntimeTuning((current) => ({
      ...current,
      ...(key === "planningMode" ? {} : { enabled: true }),
      [key]: value,
    }));
  };
  const offerCodexRestart = async () => {
    const restartNow = await confirm({
      tone: "info",
      eyebrow: "配置已经安全写入",
      title: "是否立即重启 Codex？",
      message: "config.toml、主模型目录和子代理路由在重新启动 Codex 后会完整生效。",
      detail: "选择“稍后自行重启”不会撤销保存；下次手动打开 Codex 时会自动应用新配置。",
      confirmLabel: "立即重启 Codex",
      cancelLabel: "稍后自行重启",
    });
    if (!restartNow) {
      notify("配置已保存；请稍后自行重启 Codex 以应用");
      return;
    }
    try {
      await api("/api/codex/restart", { method: "POST", body: "{}" });
      notify("Codex 已按新配置重新启动");
    } catch (error) {
      notify(`配置已保存，但 Codex 重启失败：${error.message}`, "error");
    }
  };
  const discardConfigDraft = () => {
    if (configDocument) hydrateCodexConfigDocument(configDocument, false);
  };
  const updateConfigEntryDraft = (id, value) =>
    setConfigEntryDrafts((current) => ({ ...current, [id]: value }));
  const switchConfigView = (nextView) => {
    if (nextView === configView) return;
    const activeDirty = configView === "raw" ? configRawDirty : configVisualDirty;
    if (activeDirty) {
      notify("请先保存或还原当前配置草稿，再切换编辑方式", "warning");
      return;
    }
    setConfigView(nextView);
  };
  const codexConfigPayload = () => {
    if (!configDirty) return null;
    if (configRawDirty && configVisualDirty)
      throw new Error("原始 TOML 与可视化字段同时有修改，请保留其中一种后再保存");
    const expectedFingerprint = String(configDocument?.fingerprint || "");
    return configRawDirty
      ? { content: configRaw, expectedFingerprint }
      : {
          expectedFingerprint,
          updates: (configDocument?.entries || [])
            .filter(
              (entry) =>
                entry.editable &&
                configEntryDrafts[configEntryId(entry)] !==
                  configInitialDraftsRef.current[configEntryId(entry)],
            )
            .map((entry) => ({
              path: entry.path,
              value: parseConfigEntryDraft(entry, configEntryDrafts[configEntryId(entry)]),
            })),
        };
  };
  const persistCodexConfig = async ({ silent = false } = {}) => {
    const payload = codexConfigPayload();
    if (!payload) return null;
    setConfigSaving(true);
    try {
      const response = await api("/api/codex-config", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      hydrateCodexConfigDocument(
        response.document || response.result?.document,
        true,
      );
      if (!silent)
        notify(
          response.changed
            ? "config.toml 已备份并保存，重启 Codex 后完整生效"
            : "config.toml 内容没有变化",
        );
      return response;
    } finally {
      setConfigSaving(false);
    }
  };
  const saveCodexConfigOnly = async () => {
    try {
      const result = await persistCodexConfig();
      if (result?.changed) await offerCodexRestart();
    } catch (error) {
      notify(error.message, "error");
    }
  };
  const refreshCodexConfig = async () => {
    if (configDirty) {
      const approved = await confirm({
        tone: "warning",
        title: "放弃未保存的配置修改？",
        message: "重新读取会丢弃当前 config.toml 草稿。",
        detail: "磁盘上的配置不会被修改。",
        confirmLabel: "放弃并重新读取",
      });
      if (!approved) return;
    }
    try {
      await loadCodexConfig(true);
      notify("已重新读取当前 config.toml");
    } catch (error) {
      notify(error.message, "error");
    }
  };
  const save = async () => {
    const restartRequired = dirty;
    const initialRuntimeTuning = normalizeRuntimeTuning(
      JSON.parse(initialDraftRef.current).runtimeTuning,
    );
    const runtimePatch = Object.fromEntries(
      Object.entries(runtimeTuning).filter(
        ([key, value]) => value !== initialRuntimeTuning[key],
      ),
    );
    setBusy(true);
    try {
      const applied = await api("/api/orchestration/save", {
        method: "POST",
        body: JSON.stringify({
          modelWorkspace: {
            mode,
            activeSourceId: workspace.activeSourceId,
            selectAll:
              visibleKeys.length > 0 && visibleKeys.every((key) => selected.has(key)),
            selectedModels: [...selected],
            defaultModelKey: defaultKey,
            syncToCodex: true,
          },
          subagentRouting: { ...routing, routes: routing.routes },
          runtimeTuning: runtimePatch,
          codexConfig: codexConfigPayload(),
        }),
      });
      const persistedRuntime = normalizeRuntimeTuning(applied.runtimeTuning);
      setRuntimeTuning(persistedRuntime);
      if (applied.document) hydrateCodexConfigDocument(applied.document, true);
      notify(`已同步 ${selected.size} 个主模型和四级子代理路由到 Codex`);
      if (applied.result.gatewayRequired && !data.web2apiStatus.running)
        notify("聚合服务已在后台启动");
      initialDraftRef.current = JSON.stringify({
        mode,
        selected: [...selected].sort(),
        defaultKey,
        routing,
        runtimeTuning: persistedRuntime,
      });
      runtimeTouchedRef.current = false;
      loadCodexConfig(true).catch(() => {});
      await reload();
      if (restartRequired) await offerCodexRestart();
      return true;
    } catch (error) {
      notify(error.message, "error");
      return false;
    } finally {
      setBusy(false);
    }
  };
  const hydrateDraft = (fresh) => {
    const nextWorkspace = fresh.settings.modelWorkspace;
    const nextDefault =
      nextWorkspace.defaultModelKey || fresh.selectedModelKeys[0] || "";
    setMode(nextWorkspace.mode);
    setSelected(new Set(fresh.selectedModelKeys));
    setDefaultKey(nextDefault);
    setRouting(fresh.effectiveSubagentRouting);
    const nextRuntimeTuning = normalizeRuntimeTuning(
      fresh.settings.runtimeTuning,
    );
    setRuntimeTuning(nextRuntimeTuning);
    runtimeTouchedRef.current = false;
    setAdvanced(false);
    initialDraftRef.current = JSON.stringify({
      mode: nextWorkspace.mode,
      selected: [...fresh.selectedModelKeys].sort(),
      defaultKey: nextDefault,
      routing: fresh.effectiveSubagentRouting,
      runtimeTuning: nextRuntimeTuning,
    });
  };
  const discardDraft = () => {
    const initial = JSON.parse(initialDraftRef.current);
    setMode(initial.mode);
    setSelected(new Set(initial.selected));
    setDefaultKey(initial.defaultKey);
    setRouting(initial.routing);
    setRuntimeTuning(normalizeRuntimeTuning(initial.runtimeTuning));
    runtimeTouchedRef.current = false;
    discardConfigDraft();
  };
  const refreshWorkspace = async () => {
    if (dirty) {
      const decision = await confirm({
        choiceMode: true,
        tone: "warning",
        eyebrow: "未保存的调度草稿",
        title: "刷新前如何处理当前修改？",
        message: "调度中心仍有未保存的模型或子代理配置。",
        detail: "可以先保存并同步、放弃本次修改，或取消刷新继续编辑。",
        confirmLabel: "保存并同步",
        discardLabel: "放弃修改",
      });
      if (decision === "cancel") return;
      if (decision === "save") {
        if (!(await save())) return;
      }
    }
    try {
      const fresh = await refreshModels();
      if (!fresh) return;
      hydrateDraft(fresh);
      await loadCodexConfig(true);
    } catch (error) {
      notify(error.message, "error");
    }
  };
  useEffect(() => {
    onDraftStateChange?.({ dirty, save, discard: discardDraft });
    return () => onDraftStateChange?.(null);
  }, [dirty, draftSnapshot, configRaw, configEntryDrafts, configView, onDraftStateChange]);
  const restoreDefaults = async () => {
    const approved = await confirm({
      tone: "info",
      eyebrow: "安全恢复",
      title: "恢复调度中心默认状态？",
      message:
        "主模型会切回独立模式并跟随当前账号；子代理恢复为自适应策略与安全的一级默认回退。",
      detail: "恢复结果会立即同步到 Codex，账号、分组和 API 号池不会被删除。",
      confirmLabel: "恢复并同步",
    });
    if (!approved) return;
    setBusy(true);
    try {
      await api("/api/orchestration/restore-defaults", {
        method: "POST",
        body: "{}",
      });
      const fresh = await reload();
      hydrateDraft(fresh);
      await loadCodexConfig(true);
      notify("已恢复默认调度并同步到 Codex");
      await offerCodexRestart();
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };
  const modelId = String(configDocument?.common?.modelId || "").trim();
  const modelContextDefault = Number(configDocument?.common?.modelContextDefault || 0);
  const modelContextMax = Number(configDocument?.common?.modelContextMax || 0);
  const modelContextReferenceMax = Number(configDocument?.common?.modelContextReferenceMax || 0);
  const contextInputMax = Math.max(modelContextMax, modelContextReferenceMax) || 2000000;
  const contextCurrentValue = Number(runtimeTuning.modelContextWindow || modelContextDefault || 0);
  const compactCurrentValue = Number(runtimeTuning.autoCompactTokenLimit || 0);
  const contextSliderStops = [];
  [
    { value: 0, effectiveValue: modelContextDefault || 272000, label: "模型" },
    ...[128000, modelContextDefault, 500000, modelContextMax, ...(modelContextReferenceMax >= 1000000 ? [1000000] : [])]
      .filter((value) => value > 0)
      .map((value) => ({
        value,
        effectiveValue: value,
        label: value === 1000000
          ? "1M"
          : value === modelContextMax && modelContextMax !== modelContextDefault
          ? modelContextReferenceMax > modelContextMax ? formatTokenCount(value) : "上限"
          : formatTokenCount(value),
      })),
  ]
    .sort((left, right) => left.effectiveValue - right.effectiveValue || left.value - right.value)
    .forEach((item) => {
      if (!contextSliderStops.some((existing) => existing.effectiveValue === item.effectiveValue)) {
        contextSliderStops.push(item);
      }
    });
  if (
    contextCurrentValue > 0
    && !contextSliderStops.some((item) => item.effectiveValue === contextCurrentValue)
  ) {
    contextSliderStops.push({
      value: Number(runtimeTuning.modelContextWindow),
      effectiveValue: contextCurrentValue,
      label: "当前",
    });
    contextSliderStops.sort((left, right) => left.effectiveValue - right.effectiveValue);
  }
  const contextSliderIndex = Math.max(
    0,
    contextSliderStops.findIndex((item) => item.effectiveValue === contextCurrentValue),
  );
  const compactSliderStops = [{ value: 0, label: "自动" }];
  [0.7, 0.8, 0.9]
    .map((ratio) => Math.floor((contextCurrentValue * ratio) / 1024) * 1024)
    .filter((value, index, values) => value >= 8192 && values.indexOf(value) === index)
    .forEach((value, index) => compactSliderStops.push({
      value,
      label: ["提前", "平衡", "延后"][index],
    }));
  if (
    compactCurrentValue > 0
    && !compactSliderStops.some((item) => item.value === compactCurrentValue)
  ) {
    compactSliderStops.push({ value: compactCurrentValue, label: "当前" });
    compactSliderStops.splice(
      1,
      compactSliderStops.length - 1,
      ...compactSliderStops.slice(1).sort((left, right) => left.value - right.value),
    );
  }
  const compactSliderIndex = Math.max(
    0,
    compactSliderStops.findIndex((item) => item.value === compactCurrentValue),
  );
  const compactRatio = contextCurrentValue && compactCurrentValue
    ? Math.min(100, Math.max(0, (compactCurrentValue / contextCurrentValue) * 100))
    : 0;
  const expectedContextBudget = contextBudget({
    requested: runtimeTuning.modelContextWindow,
    defaultWindow: modelContextDefault,
    nativeMax: modelContextMax,
    referenceMax: modelContextReferenceMax,
    inputReferenceMax: configDocument?.common?.modelInputReferenceMax,
    effectivePercent: configDocument?.common?.modelContextEffectivePercent,
  });
  const contextCurrentLabel = contextCurrentValue
    ? `${formatTokenCount(contextCurrentValue)} 设置${expectedContextBudget.usable != null ? ` · 预计可用 ${formatTokenCount(expectedContextBudget.usable)}` : ""} · ${compactCurrentValue ? `${formatTokenCount(compactCurrentValue)} 压缩设置` : "压缩跟随模型"}`
    : "总容量与压缩点均跟随模型";
  const webSearchCurrentLabel = {
    "": "Codex 默认（按任务决定）",
    live: "实时联网",
    cached: "缓存索引",
    indexed: "许可时联网",
    disabled: "已关闭",
  }[runtimeTuning.webSearch] || "Codex 默认（按任务决定）";
  const webSearchDepthLabel = {
    "": "深度跟随 Codex",
    low: "精简搜索",
    medium: "平衡搜索",
    high: "深入搜索",
  }[runtimeTuning.webSearchContextSize] || "深度跟随 Codex";
  const compactScopeLabel = runtimeTuning.autoCompactScope === "body_after_prefix"
    ? "仅统计正文"
    : "完整上下文";
  const mcpGraceSupported = codexVersionAtLeast(data.codexVersion, 0, 151, 0);
  const mcpGraceValue = Number.isFinite(Number(runtimeTuning.mcpOptionalStartupGraceMs))
    ? Number(runtimeTuning.mcpOptionalStartupGraceMs)
    : -1;
  const mcpGracePresets = [-1, 0, 500, 1000, 2000, 5000];
  const mcpGracePresetValue = mcpGracePresets.includes(mcpGraceValue)
    ? String(mcpGraceValue)
    : "custom";
  const mcpGraceLabel = !mcpGraceSupported
    ? "需 Codex 0.151+"
    : mcpGraceValue < 0
      ? "跟随 Codex（默认 1 秒）"
      : mcpGraceValue === 0
        ? "使用各扩展自身超时"
        : `${mcpGraceValue} 毫秒共享等待`;
  const updateContextWindow = (value) => {
    const nextContextWindow = Number(value) || 0;
    const nextEffectiveWindow = nextContextWindow || modelContextDefault || 0;
    runtimeTouchedRef.current = true;
    setRuntimeTuning((current) => ({
      ...current,
      enabled: true,
      modelContextWindow: nextContextWindow,
      autoCompactTokenLimit:
        current.autoCompactTokenLimit && nextEffectiveWindow
        && current.autoCompactTokenLimit >= nextEffectiveWindow
          ? 0
          : current.autoCompactTokenLimit,
    }));
  };
  const updateAutoCompactWindow = (value) => {
    let nextLimit = Number(value) || 0;
    if (nextLimit && contextCurrentValue && nextLimit >= contextCurrentValue) {
      nextLimit = Math.max(8192, Math.floor((contextCurrentValue - 1) / 1024) * 1024);
    }
    updateRuntimeTuning("autoCompactTokenLimit", nextLimit);
  };
  const updateWebSearchMode = (value) => {
    runtimeTouchedRef.current = true;
    setRuntimeTuning((current) => ({
      ...current,
      enabled: true,
      webSearch: value,
      webSearchContextSize: value === "disabled" ? "" : current.webSearchContextSize,
    }));
  };
  return (
    <section className="page routing-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">ORCHESTRATION</span>
          <h1>调度中心</h1>
          <p>左侧控制 Codex 主模型目录，右侧配置子代理的难度与三级回退。</p>
        </div>
        <div className="heading-actions routing-actions">
          <button className="button secondary" onClick={refreshWorkspace} disabled={busy}>
            <RefreshCw size={17} />
            手动刷新
          </button>
          <button
            className="button secondary"
            onClick={restoreDefaults}
            disabled={busy}
          >
            <RotateCcw size={17} />
            恢复默认
          </button>
          <button
            className="button primary save-all"
            onClick={save}
            disabled={busy}
          >
            {busy ? <Loader2 className="spin" size={17} /> : <Save size={17} />}
            保存并同步到 Codex
          </button>
        </div>
      </div>
      {runtimeStatus && runtimeStatus.available === false && (
        <div className="runtime-repair-banner" role="status">
          <AlertTriangle size={19} />
          <span>
            <strong>子代理运行时尚未就绪</strong>
            <small>
              一键修复会先扫描 Codex Desktop 内置原生运行时；仍缺失时自动下载并校验官方 Windows 运行时，不写入 CODEX_CLI_PATH。
            </small>
          </span>
          <button
            className="button primary"
            onClick={repairRuntime}
            disabled={runtimeRepairing}
          >
            {runtimeRepairing ? <Loader2 className="spin" size={16} /> : <Wrench size={16} />}
            全自动修复
          </button>
        </div>
      )}
      <div className="orchestration-grid">
        <section className="orchestration-card main-model-card">
          <header className="card-heading">
            <span className="heading-icon">
              <Layers3 size={20} />
            </span>
            <div>
              <h2>主模型设置</h2>
              <p>
                {mode === "aggregate"
                  ? "聚合所有导入账号的模型"
                  : "只使用首页当前选择的账号"}
              </p>
            </div>
          </header>
          <div className="mode-strip">
            <button
              className={mode === "independent" ? "active" : ""}
              onClick={() => setMode("independent")}
            >
              <ShieldCheck size={16} />
              <span>
                <strong>独立模式</strong>
                <small>跟随当前账号</small>
              </span>
            </button>
            <button
              className={mode === "aggregate" ? "active" : ""}
              onClick={() => setMode("aggregate")}
            >
              <Layers3 size={16} />
              <span>
                <strong>聚合模式</strong>
                <small>跨账号统一路由</small>
              </span>
            </button>
          </div>
          <div className="selection-summary">
            <span>{visibleSelectedCount} 个模型已勾选</span>
            <button onClick={refreshModels} disabled={busy || modelsRefreshing} title="重新读取当前账号的模型目录，保留未保存的选择">
              {modelsRefreshing ? "刷新中…" : "刷新模型"}
            </button>
            <button onClick={selectVisible}>全选</button>
            <button onClick={clearVisible}>清空</button>
          </div>
          <ModelSourcePicker
            sources={data.modelSources}
            selected={selected}
            setSelected={setSelected}
            defaultKey={defaultKey}
            setDefaultKey={setDefaultKey}
            aggregate={mode === "aggregate"}
          />
          <p className="card-footnote">
            <CircleGauge size={15} />
            同步后可在 Codex
            模型选择器内直接切换；聚合模式由本地加密网关按模型路由。
          </p>
        </section>
        <section className="orchestration-card subagent-card">
          <header className="card-heading">
            <span className="heading-icon violet">
              <Route size={20} />
            </span>
            <div>
              <h2>子代理设置</h2>
              <p>策略决定何时调用；同级模型按顺序自动回退</p>
            </div>
          </header>
          {routing.strategyId !== "verification_first" && <div className={cx("subagent-runtime-note", sharedSubagentGateway && "gateway")}>
            {sharedSubagentGateway ? <Route size={17} /> : <ShieldCheck size={17} />}
            <span><strong>{sharedSubagentGateway ? "共享本地路由" : "使用已配置的子代理路由"}</strong><small>{sharedSubagentGateway ? "主模型和子代理都通过本机统一入口请求，并各自绑定所选账号。配置变更后需重新载入 Codex。" : "各档主模型思考强度均加载此处的难度路由和策略提示词。"}</small></span>
          </div>}
          {routing.strategyId === "verification_first" && <div className="subagent-runtime-note"><ShieldCheck size={17} /><span><strong>跟随 Codex 原生行为</strong><small>不接管子代理设置；是否调用、使用哪些原生子代理由 Codex 与当前模型决定。</small></span></div>}
          <div className="strategy-picks">
            {data.settings.strategies.map((strategy) => (
              <button
                className={cx(
                  `strategy-${strategy.id}`,
                  routing.strategyId === strategy.id && "active",
                )}
                onClick={() => chooseStrategy(strategy)}
                key={strategy.id}
              >
                <span>
                  {routing.strategyId === strategy.id && <Check size={13} />}
                </span>
                <strong>{strategy.name}</strong>
                <small>{strategy.description}</small>
              </button>
            ))}
          </div>
          <button
            className={cx("advanced-toggle", advanced && "active")}
            onClick={() => setAdvanced(!advanced)}
            aria-expanded={advanced}
            disabled={routing.strategyId === "verification_first"}
          >
            <span>
              <Sparkles size={17} />
              <strong>高级设置</strong>
                  <small>
                    {routing.strategyId === "verification_first"
                      ? "原生模式不接管难度路由和调用提示词"
                      : "难度路由、三级模型回退和调用提示词"}
                  </small>
            </span>
            {advanced ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
          </button>
        </section>
          {advanced && (
            <div className="advanced-panel orchestration-routes-panel">
              <div className="advanced-heading">
                <div>
                  <h3>难度路由</h3>
                  <p>
                    无需填写判定条件。每个候选模型可独立设置思考程度；首选不可用时依次尝试备用
                    1、备用 2。
                  </p>
                </div>
              </div>
              <div className="difficulty-routes">
                {levels.map((level) => {
                  const usedKeys = new Set(routing.routes[level]?.models || []);
                  return (
                    <section
                      className={`difficulty-row level-${level}`}
                      key={level}
                    >
                      <header>
                        <span>{levelLabels[level]}</span>
                        <div>
                          <strong>{levelLabels[level]}任务</strong>
                          <small>{levelHints[level]}</small>
                        </div>
                      </header>
                      <div className="fallback-grid">
                        {[0, 1, 2].map((slot) => (
                          <RouteSelect
                            key={slot}
                            slot={slot}
                            value={routing.routes[level]?.models?.[slot]}
                            effort={
                              routing.routes[level]?.efforts?.[slot] || ""
                            }
                            onChange={(key) => updateRoute(level, slot, key)}
                            onEffortChange={(effort) =>
                              updateRouteEffort(level, slot, effort)
                            }
                            sources={usableSources}
                            usedKeys={usedKeys}
                            allEfforts={data.efforts || []}
                          />
                        ))}
                      </div>
                    </section>
                  );
                })}
              </div>
              <label className="field prompt-field">
                <span>
                  调用策略提示词{" "}
                  <small>此处显示策略正文；同步时会附加调用规则、生命周期和实际路由</small>
                </span>
                <textarea
                  aria-label="调用策略提示词"
                  rows={10}
                  value={routing.prompt}
                  readOnly={routing.strategyId !== "parallel_first"}
                  onChange={(event) =>
                    setRouting({ ...routing, prompt: event.target.value })
                  }
                />
              </label>
              <p className="card-footnote">
                <ShieldCheck size={15} />
                {routing.strategyId === "parallel_first"
                  ? "自定义提示词负责判断何时、调用哪个等级；Agent 注册与失败回退由软件生成。"
                  : "当前模式使用经过优化的固定策略，避免无意义委派和重复消耗 Token；选择“自定义模式”后才可编辑。"}
                Codex“说明”展示同步后的完整内容，因此篇幅不同，策略正文应保持一致。
              </p>
            </div>
          )}
        <section className="orchestration-card runtime-tuning-card">
          <header className="card-heading">
            <span className="heading-icon runtime">
              <Gauge size={20} />
            </span>
            <div>
              <h2>Codex 配置文件</h2>
              <p>读取并编辑当前 config.toml，常用项与原始内容保持同步</p>
            </div>
            <div className="runtime-heading-actions">
              <ConfigRecoveryPanel api={api} notify={notify} confirm={confirm} disabled={busy || configSaving || configLoading} hasUnsavedDraft={configDirty} onRestored={async () => { await loadCodexConfig(true); await reload(); }} />
              <a
                className="runtime-doc-link"
                href="https://learn.chatgpt.com/docs/config-file/config-reference"
                target="_blank"
                rel="noreferrer"
              >
                配置参考 <ExternalLink size={14} />
              </a>
            </div>
          </header>
          <div className="runtime-quick-grid">
            <div className="runtime-quick-card runtime-context-card">
              <span className="runtime-quick-icon"><Gauge size={18} /></span>
              <span className="runtime-quick-copy">
                <strong>上下文与自动压缩</strong>
                <small>{expectedContextBudget.usable != null
                  ? `${configDocument?.common?.modelInputReferenceMax ? `模型总上下文 ${formatTokenCount(modelContextReferenceMax)}，最大输入 ${formatTokenCount(configDocument.common.modelInputReferenceMax)}；` : ""}Codex 另预留 ${expectedContextBudget.reservedPercent}%。设置值受输入上限约束，保存同步后需让 Codex 重新加载配置。`
                  : "上下文是总容量；当前来源未提供可用比例，Codex 实际容量以运行时报告为准。"}</small>
                {expectedContextBudget.capped && <small>当前设置超过已知输入范围；预计按 {formatTokenCount(expectedContextBudget.total)} 输入窗口生效。</small>}
              </span>
              <span className="runtime-current-state"><em>当前</em><strong>{contextCurrentLabel}</strong></span>
              <div className="runtime-linked-controls">
                <div className="runtime-control-field runtime-slider-field">
                  <span className="runtime-control-label">
                    <span>上下文窗口设置</span>
                    <em>{runtimeTuning.modelContextWindow ? "自定义" : "跟随模型"}</em>
                  </span>
                  <DiscreteSlider stops={contextSliderStops} selectedIndex={contextSliderIndex} onSelect={stop => updateContextWindow(stop?.value || 0)} ariaLabel="按档位选择 Codex 上下文总容量" markPrefix="将上下文总容量设为" valueText={contextSliderStops[contextSliderIndex]?.label || formatTokenCount(contextCurrentValue)}>
                    <label className="runtime-slider-number">
                      <input
                        type="number"
                        min="16384"
                        max={contextInputMax}
                        step="1"
                        value={contextCurrentValue || ""}
                        aria-label="精确输入模型上下文总容量"
                        onChange={(event) => updateContextWindow(Number(event.target.value))}
                      />
                      <span>Token</span>
                    </label>
                  </DiscreteSlider>
                </div>
                <div className="runtime-control-field runtime-slider-field">
                  <span className="runtime-control-label">
                    <span>自动压缩点</span>
                    <em>{compactCurrentValue ? `${Math.round(compactRatio)}%` : "自动"}</em>
                  </span>
                  <DiscreteSlider stops={compactSliderStops} selectedIndex={compactSliderIndex} onSelect={stop => updateAutoCompactWindow(stop?.value || 0)} ariaLabel="按档位选择 Codex 自动压缩阈值" markPrefix="将自动压缩点设为" valueText={compactCurrentValue ? formatTokenCount(compactCurrentValue) : "跟随 Codex"}>
                    <label className="runtime-slider-number">
                      <input
                        type="number"
                        min="8192"
                        max={Math.max(8192, contextCurrentValue - 1)}
                        step="1024"
                        placeholder="自动"
                        value={compactCurrentValue || ""}
                        aria-label="精确输入自动压缩阈值，留空则跟随 Codex"
                        onChange={(event) => updateAutoCompactWindow(Number(event.target.value))}
                      />
                      <span>Token</span>
                    </label>
                  </DiscreteSlider>
                </div>
              </div>
              <div className="runtime-scope-control">
                <span><strong>压缩统计范围</strong><small>选择压缩时的统计范围；压缩点跟随模型时也可设置。</small></span>
                <RefinedSelect variant="field" value={runtimeTuning.autoCompactScope} ariaLabel="选择自动压缩统计范围"
                  onChange={value => updateRuntimeTuning("autoCompactScope",value)}
                  options={[{value:"total",label:"完整上下文"},{value:"body_after_prefix",label:"固定前缀后的正文"}]} />
                <em>{compactScopeLabel}</em>
              </div>
            </div>
            <div className="runtime-quick-card runtime-search-card">
              <span className="runtime-quick-icon"><Globe2 size={18} /></span>
              <span className="runtime-quick-copy">
                <strong>联网搜索与深度</strong>
                <small>先决定是否联网，再决定一次搜索读取多少资料；普通使用建议都跟随 Codex。</small>
              </span>
              <span className="runtime-current-state"><em>当前</em><strong>{webSearchCurrentLabel} · {runtimeTuning.webSearch === "disabled" ? "深度停用" : webSearchDepthLabel}</strong></span>
              <div className="runtime-search-controls">
                <label className="runtime-control-field">
                  <span>联网方式</span>
                  <select
                    value={runtimeTuning.webSearch}
                    aria-label="选择 Codex 联网搜索方式"
                    onChange={(event) => updateWebSearchMode(event.target.value)}
                  >
                    <option value="">Codex 默认（按任务决定）</option>
                    <option value="live">实时联网（优先最新网页）</option>
                    <option value="cached">缓存索引（更快、更稳定）</option>
                    <option value="indexed">许可时联网</option>
                    <option value="disabled">关闭联网搜索</option>
                  </select>
                </label>
                <label className={cx("runtime-control-field", runtimeTuning.webSearch === "disabled" && "disabled")}>
                  <span>搜索深度</span>
                  <select
                    value={runtimeTuning.webSearchContextSize}
                    disabled={runtimeTuning.webSearch === "disabled"}
                    aria-label="选择 Codex 联网搜索深度"
                    onChange={(event) => updateRuntimeTuning("webSearchContextSize", event.target.value)}
                  >
                    <option value="">跟随 Codex（推荐）</option>
                    <option value="low">精简 · 更快、读取更少</option>
                    <option value="medium">平衡 · 日常推荐</option>
                    <option value="high">深入 · 更多资料</option>
                  </select>
                </label>
              </div>
            </div>
            <div className="runtime-quick-card runtime-quick-switch">
              <span className="runtime-quick-icon"><Zap size={18} /></span>
              <span className="runtime-quick-copy">
                <strong>Fast 加速</strong>
                <small>支持的模型会更快，但额度或费用消耗更高。</small>
              </span>
              <span className="runtime-current-state"><em>当前</em><strong>{runtimeTuning.serviceTier === "fast" ? "Fast 加速" : "标准速度"}</strong></span>
              <Switch
                checked={runtimeTuning.serviceTier === "fast"}
                onChange={(checked) => updateRuntimeTuning("serviceTier", checked ? "fast" : "")}
                label={runtimeTuning.serviceTier === "fast" ? "已开启" : "标准速度"}
                ariaLabel="Fast 加速"
              />
            </div>
            <div className="runtime-quick-card runtime-quick-switch">
              <span className="runtime-quick-icon"><ShieldCheck size={18} /></span>
              <span className="runtime-quick-copy">
                <strong>代理重连修复</strong>
                <small>VPN 阻断 WebSocket、反复重连时再开；正常网络保持关闭。</small>
              </span>
              <span className="runtime-current-state"><em>当前</em><strong>{runtimeTuning.vpnCompatibility ? "兼容模式已开启" : "正常网络模式"}</strong></span>
              <Switch
                checked={runtimeTuning.vpnCompatibility}
                onChange={(checked) => updateRuntimeTuning("vpnCompatibility", checked)}
                label={runtimeTuning.vpnCompatibility ? "已修复" : "按需开启"}
                ariaLabel="代理重连修复"
              />
            </div>
            <div className="runtime-quick-card runtime-quick-switch runtime-planning-card">
              <span className="runtime-quick-icon"><BrainCircuit size={18} /></span>
              <span className="runtime-quick-copy">
                <strong>任务前规划与选项</strong>
                <small>开启后先形成短计划，仅在重要方向不明确时给出选项，不会切换到 Plan 模式。</small>
              </span>
              <span className="runtime-current-state"><em>当前</em><strong>{runtimeTuning.planningMode !== "auto" ? "先规划再执行" : "直接执行"}</strong></span>
              <Switch
                checked={runtimeTuning.planningMode !== "auto"}
                onChange={(checked) => updateRuntimeTuning("planningMode", checked ? "plan_first" : "auto")}
                label={runtimeTuning.planningMode !== "auto" ? "已开启" : "直接执行"}
                ariaLabel="任务前规划与选项"
              />
            </div>
            <div className="runtime-quick-card runtime-latest-card">
              <span className="runtime-quick-icon"><PlugZap size={18} /></span>
              <span className="runtime-quick-copy">
                <strong>长任务与扩展启动</strong>
                <small>长任务运行时可阻止电脑自动休眠；可选 MCP 只共享一小段启动等待，避免慢扩展拖住整个 Codex。</small>
              </span>
              <span className="runtime-current-state">
                <em>当前</em>
                <strong>{runtimeTuning.preventIdleSleep ? "长任务防休眠" : "允许系统休眠"} · {mcpGraceLabel}</strong>
              </span>
              <div className="runtime-latest-controls">
                <div className="runtime-latest-toggle">
                  <span>
                    <strong>运行任务时防休眠</strong>
                    <small>只在 Codex 正在执行任务时生效，空闲后恢复系统原设置。</small>
                  </span>
                  <Switch
                    checked={runtimeTuning.preventIdleSleep}
                    onChange={(checked) => updateRuntimeTuning("preventIdleSleep", checked)}
                    label={runtimeTuning.preventIdleSleep ? "已开启" : "按需开启"}
                    ariaLabel="Codex 运行任务时防止系统休眠"
                  />
                </div>
                <div className={cx("runtime-mcp-grace", !mcpGraceSupported && "disabled") }>
                  <span>
                    <strong>可选 MCP 共享启动等待</strong>
                    <small>{mcpGraceSupported ? "只影响标记为 optional 的 MCP；必需扩展仍使用各自超时。" : `当前 ${data.codexVersion || "Codex 版本未知"}，更新到 0.151+ 后可用。`}</small>
                  </span>
                  <label>
                    <select
                      disabled={!mcpGraceSupported}
                      value={mcpGracePresetValue}
                      aria-label="选择可选 MCP 的共享启动等待档位"
                      onChange={(event) => {
                        const value = event.target.value;
                        updateRuntimeTuning(
                          "mcpOptionalStartupGraceMs",
                          value === "custom" ? (mcpGraceValue >= 0 ? mcpGraceValue : 1500) : Number(value),
                        );
                      }}
                    >
                      <option value="-1">跟随 Codex（推荐 · 1000ms）</option>
                      <option value="0">关闭共享等待</option>
                      <option value="500">快速 · 500ms</option>
                      <option value="1000">标准 · 1000ms</option>
                      <option value="2000">兼容 · 2000ms</option>
                      <option value="5000">慢扩展 · 5000ms</option>
                      <option value="custom">自定义</option>
                    </select>
                    <span className="runtime-slider-number">
                      <input
                        type="number"
                        min="0"
                        max="60000"
                        step="100"
                        disabled={!mcpGraceSupported}
                        placeholder="默认"
                        value={mcpGraceValue >= 0 ? mcpGraceValue : ""}
                        aria-label="精确输入可选 MCP 共享启动等待毫秒数"
                        onChange={(event) => updateRuntimeTuning(
                          "mcpOptionalStartupGraceMs",
                          event.target.value === "" ? -1 : Number(event.target.value),
                        )}
                      />
                      <span>ms</span>
                    </span>
                  </label>
                </div>
              </div>
            </div>
          </div>
          <details className="config-document-editor">
            <summary className="config-document-summary">
              <span>
                <Pencil size={15} />
                <span>
                  <strong>高级：完整 config.toml</strong>
                  <small>新手无需展开；这里只为熟悉 TOML 的用户保留完整编辑能力。</small>
                </span>
              </span>
              <span>
                {configLoading ? "正在读取" : formatBytes(configDocument?.size || 0)}
                <ChevronDown size={15} />
              </span>
            </summary>
            <div className="config-document-body">
            <header className="config-document-heading">
              <div>
                <strong>当前配置内容</strong>
                <span>
                  <code title={configDocument?.path || data.configPath}>
                    {configDocument?.path || data.configPath || "~/.codex/config.toml"}
                  </code>
                  <small>
                    {configLoading
                      ? "正在读取"
                      : `${formatBytes(configDocument?.size || 0)}${configDocument?.modifiedAt ? ` · ${formatDateTime(configDocument.modifiedAt)}` : ""}`}
                  </small>
                </span>
              </div>
              <div className="config-document-actions">
                <button
                  className="button secondary compact"
                  onClick={refreshCodexConfig}
                  disabled={configLoading || configSaving || busy}
                >
                  <RefreshCw className={configLoading ? "spin" : ""} size={14} />
                  重新读取
                </button>
                <button
                  className="button secondary compact"
                  onClick={discardConfigDraft}
                  disabled={!configDirty || configSaving || busy}
                >
                  <RotateCcw size={14} />
                  还原草稿
                </button>
                <button
                  className="button primary compact"
                  onClick={saveCodexConfigOnly}
                  disabled={!configDirty || configSaving || busy}
                >
                  {configSaving ? <Loader2 className="spin" size={14} /> : <Save size={14} />}
                  保存文件
                </button>
              </div>
            </header>
            {configError && (
              <div className="config-document-error" role="alert">
                <AlertTriangle size={16} />
                <span>{configError}</span>
              </div>
            )}
            <div className="config-editor-toolbar">
              <div className="config-view-tabs" role="tablist" aria-label="配置编辑方式">
                <button
                  className={cx(configView === "visual" && "active")}
                  hidden
                  role="tab"
                  aria-selected={configView === "visual"}
                  disabled={!configDocument?.valid}
                  onClick={() => switchConfigView("visual")}
                >
                  <ListOrdered size={14} />
                  可视化字段
                </button>
                <button
                  className={cx(configView === "raw" && "active")}
                  role="tab"
                  aria-selected={configView === "raw"}
                  onClick={() => switchConfigView("raw")}
                >
                  <Pencil size={14} />
                  原始 TOML
                </button>
              </div>
              <span className={cx("config-dirty-state", configDirty && "dirty")}>
                {configDirty ? "有未保存修改" : "已与磁盘同步"}
              </span>
            </div>
            {configLoading ? (
              <div className="config-document-loading">
                <Loader2 className="spin" size={18} />
                正在读取完整 config.toml
              </div>
            ) : configView === "raw" ? (
              <label className="config-raw-editor">
                <span>完整 TOML 内容</span>
                <textarea
                  value={configRaw}
                  spellCheck={false}
                  aria-label="完整 config.toml 内容"
                  onChange={(event) => setConfigRaw(event.target.value)}
                />
                <small>保存前会校验 TOML、创建备份并原子写入；格式错误时不会覆盖原文件。</small>
              </label>
            ) : configSections.length ? (
              <div className="config-section-list">
                {configSections.map(([section, entries]) => (
                  <section className="config-field-section" key={section}>
                    <header>
                      <strong>{configSectionLabel(section)}</strong>
                      <small>{section !== "root" && <code>{section}</code>}{section !== "root" && " · "}{entries.length} 个字段</small>
                    </header>
                    <div>
                      {entries.map((entry) => {
                        const id = configEntryId(entry);
                        return (
                          <div className="config-field-row" key={id}>
                            <span className="config-field-copy">
                              <span><strong>{entry.label}</strong><em>{entry.type}</em></span>
                              <code>{entry.key}</code>
                              <small>{entry.description}</small>
                            </span>
                            <span className="config-field-control">
                              <ConfigEntryEditor
                                entry={entry}
                                value={configEntryDrafts[id] ?? configEntryDraft(entry)}
                                onChange={updateConfigEntryDraft}
                              />
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  </section>
                ))}
              </div>
            ) : (
              <div className="config-document-empty">
                <Settings size={20} />
                <span><strong>当前配置文件没有字段</strong><small>可切换到原始 TOML 添加官方配置。</small></span>
              </div>
            )}
            </div>
          </details>
        </section>
      </div>
      <button className="session-entry-card" onClick={() => setSessionsOpen(true)}>
        <span className="session-entry-icon"><MessageSquareText size={22} /></span>
        <span className="session-entry-copy">
          <strong>会话管理</strong>
          <em>搜索、整理与同步 Codex 会话</em>
        </span>
        <span className="session-entry-meta">
          <b>{sessionHistory?.summary?.total ?? sessionHistory?.threads?.length ?? "按需读取"}</b>
          <ChevronRight size={18} />
        </span>
      </button>
      <UsageView data={data} notify={notify} confirm={confirm} embedded />
      {sessionsOpen && (
        <Modal
          title="会话管理"
          description="会话索引仅在首次打开时读取，之后由你手动刷新。"
          onClose={() => setSessionsOpen(false)}
          wide
          className="session-manager-modal"
        >
          <SessionsView
            notify={notify}
            confirm={confirm}
            initialHistory={sessionHistory}
            onHistoryChange={onSessionHistoryChange}
            embedded
          />
        </Modal>
      )}
    </section>
  );
}


function SessionsView({ notify, confirm, initialHistory, onHistoryChange, embedded = false }) {
  const [history, setHistory] = useState(() => initialHistory || null);
  const [loading, setLoading] = useState(() => !initialHistory);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState("active");
  const [query, setQuery] = useState("");
  const [displayCount, setDisplayCount] = useState(60);
  const [selected, setSelected] = useState(() => new Set());
  const [renameItem, setRenameItem] = useState(null);
  const [renameValue, setRenameValue] = useState("");
  const [syncOpen, setSyncOpen] = useState(false);
  const load = useCallback(async (force = false) => {
    setLoading(true);
    try {
      const nextHistory = await api("/api/history");
      setHistory(nextHistory);
      onHistoryChange?.(nextHistory);
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setLoading(false);
    }
  }, [notify, onHistoryChange]);
  useEffect(() => {
    if (initialHistory && !history) {
      setHistory(initialHistory);
      setLoading(false);
    }
  }, [initialHistory, history]);
  useEffect(() => {
    if (!initialHistory) load();
  }, [initialHistory, load]);
  const threads = history?.threads || [];
  const matching = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase("zh-CN");
    return threads
      .filter((item) => (tab === "archived" ? item.archived : !item.archived))
      .filter(
        (item) =>
          !needle ||
          [item.name, item.preview, item.cwd, item.provider].some((value) =>
            String(value || "")
              .toLocaleLowerCase("zh-CN")
              .includes(needle),
          ),
      )
      .sort((left, right) => Number(right.pinned) - Number(left.pinned));
  }, [threads, tab, query]);
  const visible = matching.slice(0, displayCount);
  const selectedVisible = visible.filter((item) => selected.has(item.id));
  const toggle = (id) =>
    setSelected((current) => {
      const next = new Set(current);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  const perform = async (action, ids, message) => {
    if (!ids.length) return;
    if (action === "archive") {
      const approved = await confirm({
        tone: "warning",
        title: `将 ${ids.length} 个会话移到归档？`,
        message: "归档后的会话不会从磁盘删除，可以随时在“已归档”中恢复。",
        confirmLabel: "移到归档",
      });
      if (!approved) return;
    }
    setBusy(true);
    try {
      const response = await api("/api/sessions/action", {
        method: "POST",
        body: JSON.stringify({ action, threadIds: ids }),
      });
      const outcome = response.result || {};
      const unresolved = [...(outcome.failed || []), ...(outcome.unconfirmed || [])];
      setSelected(new Set(unresolved.map(item => item.threadId)));
      await load();
      notify(unresolved.length ? `已确认 ${outcome.changed || 0} 项成功，${unresolved.length} 项失败或待核对；已保留选择` : message, unresolved.length ? "warning" : "success");
    } catch (error) {
      await load().catch(() => {});
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };
  const rename = async () => {
    setBusy(true);
    try {
      await api("/api/sessions/rename", {
        method: "POST",
        body: JSON.stringify({ threadId: renameItem.id, name: renameValue }),
      });
      setRenameItem(null);
      await load();
      notify("会话名称已更新");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };
  const inventory = history?.inventory || {};
  return (
    <>
      <section className={cx("sessions-page", embedded ? "embedded-sessions-page" : "page")}>
        <div className="page-heading">
          <div>
            <span className="eyebrow">SESSION LIBRARY</span>
            <h1>会话管理</h1>
            <p>
              集中搜索、重命名与归档；置顶保存在管理器内，可预览备份到指定同步文件夹。
            </p>
          </div>
          <div className="heading-actions">
            <button
              className="button secondary"
              onClick={() => setSyncOpen(true)}
              disabled={!history}
            >
              <RefreshCw size={16} />
              会话同步
            </button>
            <button
              className="button secondary"
              onClick={load}
              disabled={loading || busy}
            >
              {loading ? (
                <Loader2 className="spin" size={16} />
              ) : (
                <RefreshCw size={16} />
              )}
              刷新列表
            </button>
          </div>
        </div>
        <SessionRepairPanel api={api} confirm={confirm} notify={notify} onRecovered={load} busy={busy} onBusyChange={setBusy} />
        <div className="session-overview">
          <div>
            <MessageSquareText size={19} />
            <span>
              <small>可管理会话</small>
              <strong>{threads.filter((item) => !item.archived).length}</strong>
            </span>
          </div>
          <div>
            <Archive size={19} />
            <span>
              <small>已归档</small>
              <strong>{threads.filter((item) => item.archived).length}</strong>
            </span>
          </div>
          <div>
            <Layers3 size={19} />
            <span>
              <small>本地索引</small>
              <strong>{inventory.stateThreads ?? "--"}</strong>
            </span>
          </div>
          <div>
            <Download size={19} />
            <span>
              <small>会话体积</small>
              <strong>{formatBytes(inventory.totalBytes)}</strong>
            </span>
          </div>
        </div>
        <div className="session-controls">
          <div className="session-tabs">
            <button
              className={tab === "active" ? "active" : ""}
              onClick={() => {
                setTab("active");
                setSelected(new Set());
                setDisplayCount(60);
              }}
            >
              <MessageSquareText size={15} />
              当前会话{" "}
              <span>{threads.filter((item) => !item.archived).length}</span>
            </button>
            <button
              className={tab === "archived" ? "active" : ""}
              onClick={() => {
                setTab("archived");
                setSelected(new Set());
                setDisplayCount(60);
              }}
            >
              <Archive size={15} />
              已归档{" "}
              <span>{threads.filter((item) => item.archived).length}</span>
            </button>
          </div>
          <label className="session-search">
            <Search size={16} />
            <input
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setDisplayCount(60);
              }}
              placeholder="搜索标题、内容或工作目录"
            />
          </label>
        </div>
        {!!selectedVisible.length && (
          <div className="session-bulk">
            <div>
              <CheckSquare size={17} />
              <strong>已选择 {selectedVisible.length} 个会话</strong>
            </div>
            {tab === "active" ? (
              <>
                <button
                  className="button subtle"
                  onClick={() =>
                    perform(
                      "pin",
                      selectedVisible.map((item) => item.id),
                      "所选会话已置顶",
                    )
                  }
                >
                  <Pin size={15} />
                  置顶
                </button>
                <button
                  className="button danger-outline"
                  onClick={() =>
                    perform(
                      "archive",
                      selectedVisible.map((item) => item.id),
                      "所选会话已归档",
                    )
                  }
                >
                  <Archive size={15} />
                  归档
                </button>
              </>
            ) : (
              <button
                className="button primary"
                onClick={() =>
                  perform(
                    "restore",
                    selectedVisible.map((item) => item.id),
                    "所选会话已恢复",
                  )
                }
              >
                <RotateCcw size={15} />
                恢复
              </button>
            )}
          </div>
        )}
        {history?.threadError && (
          <div className="session-error">
            <AlertTriangle size={18} />
            <div>
              <strong>暂时无法读取会话索引</strong>
              <span>{history.threadError}</span>
            </div>
            <button className="button secondary" onClick={reindex}>
              尝试重建
            </button>
          </div>
        )}
        <div className="session-list-heading">
          <label>
            <input
              type="checkbox"
              checked={
                visible.length > 0 &&
                visible.every((item) => selected.has(item.id))
              }
              onChange={(event) =>
                setSelected(
                  event.target.checked
                    ? new Set(visible.map((item) => item.id))
                    : new Set(),
                )
              }
            />
            <span>选择当前列表</span>
          </label>
          <em>
            {matching.length} 个会话
            {visible.length < matching.length
              ? ` · 已显示 ${visible.length}`
              : ""}
          </em>
        </div>
        <div className="session-list">
          {visible.map((item) => (
            <article
              className={cx(
                "session-row",
                item.pinned && "pinned",
                selected.has(item.id) && "selected",
              )}
              key={`${item.archived}-${item.id}`}
            >
              <label className="session-check">
                <input
                  type="checkbox"
                  checked={selected.has(item.id)}
                  onChange={() => toggle(item.id)}
                />
                <span />
              </label>
              <span className="session-kind">
                {item.pinned ? (
                  <Pin size={17} />
                ) : item.archived ? (
                  <Archive size={17} />
                ) : (
                  <MessageSquareText size={17} />
                )}
              </span>
              <div className="session-copy">
                <strong>{item.name || "未命名会话"}</strong>
                {item.preview && item.preview !== item.name && (
                  <p>{item.preview}</p>
                )}
                <div>
                  <span>{item.provider || "openai"}</span>
                  <span>{item.source || "unknown"}</span>
                  {item.cwd && <span title={item.cwd}>{item.cwd}</span>}
                </div>
              </div>
              <time>{formatTime(item.updatedAt || item.createdAt)}</time>
              <div className="session-actions">
                <IconButton
                  label="重命名"
                  disabled={busy}
                  onClick={() => {
                    setRenameItem(item);
                    setRenameValue(item.name || "");
                  }}
                >
                  <Pencil size={15} />
                </IconButton>
                {!item.archived && (
                  <IconButton
                    label={item.pinned ? "取消置顶" : "置顶"}
                    disabled={busy}
                    onClick={() =>
                      perform(
                        item.pinned ? "unpin" : "pin",
                        [item.id],
                        item.pinned ? "已取消置顶" : "会话已置顶",
                      )
                    }
                  >
                    <Pin size={15} />
                  </IconButton>
                )}
                {item.archived ? (
                  <IconButton
                    label="恢复会话"
                    disabled={busy}
                    onClick={() => perform("restore", [item.id], "会话已恢复")}
                  >
                    <RotateCcw size={15} />
                  </IconButton>
                ) : (
                  <IconButton
                    label="归档会话"
                    className="danger"
                    disabled={busy}
                    onClick={() => perform("archive", [item.id], "会话已归档")}
                  >
                    <Archive size={15} />
                  </IconButton>
                )}
              </div>
            </article>
          ))}
          {visible.length < matching.length && (
            <button
              className="session-load-more"
              onClick={() => setDisplayCount((value) => value + 60)}
            >
              加载更多 <span>剩余 {matching.length - visible.length}</span>
            </button>
          )}
          {!loading && !visible.length && !history?.threadError && (
            <div className="session-empty">
              <MessageSquareText size={27} />
              <strong>
                {query
                  ? "没有匹配的会话"
                  : tab === "archived"
                    ? "归档里还没有会话"
                    : "暂时没有可管理的会话"}
              </strong>
              <span>
                {query
                  ? "换个关键词试试。"
                  : "在 Codex 中创建任务后，会话会自动显示在这里。"}
              </span>
            </div>
          )}
          {loading && (
            <div className="session-loading">
              <Loader2 className="spin" size={20} />
              正在读取会话索引…
            </div>
          )}
        </div>
      </section>
      {renameItem && (
        <Modal
          title="重命名会话"
          description="名称会同步到 Codex 的任务列表"
          onClose={() => setRenameItem(null)}
        >
          <div className="modal-body form-stack">
            <label className="field">
              <span>会话名称</span>
              <input
                autoFocus
                maxLength="160"
                value={renameValue}
                onChange={(event) => setRenameValue(event.target.value)}
                onKeyDown={(event) =>
                  event.key === "Enter" && renameValue.trim() && rename()
                }
              />
            </label>
            <button
              className="button primary full"
              disabled={busy || !renameValue.trim()}
              onClick={rename}
            >
              {busy ? (
                <Loader2 className="spin" size={16} />
              ) : (
                <Save size={16} />
              )}
              保存名称
            </button>
          </div>
        </Modal>
      )}
      {syncOpen && history && (
        <SessionSyncModal
          api={api}
          formatBytes={formatBytes}
          history={history}
          onClose={() => setSyncOpen(false)}
          onDone={load}
          notify={notify}
        />
      )}
    </>
  );
}

function ToolPagination({ page, pageCount, onChange, label }) {
  if (pageCount <= 1) return null;
  return (
    <nav className="tool-pagination" aria-label={label}>
      <button className="icon-button" aria-label="上一页" disabled={page <= 1} onClick={() => onChange(page - 1)}><ChevronLeft size={16} /></button>
      <span>第 {page} / {pageCount} 页</span>
      <button className="icon-button" aria-label="下一页" disabled={page >= pageCount} onClick={() => onChange(page + 1)}><ChevronRight size={16} /></button>
    </nav>
  );
}

function ToolboxView({ notify, confirm, data, updateData }) {
  const PAGE_SIZE = 6;
  const MESSAGE_PAGE_SIZE = 10;
  const [tool, setTool] = useState("totp");
  const [skillsOpened, setSkillsOpened] = useState(false);
  const [radarOpened, setRadarOpened] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState({ totpItems: [], mailAccounts: [] });
  const [clock, setClock] = useState(Date.now());
  const [totpInput, setTotpInput] = useState("");
  const [totpLabel, setTotpLabel] = useState("");
  const [saveTotp, setSaveTotp] = useState(false);
  const [totpResult, setTotpResult] = useState(null);
  const [editingTotp, setEditingTotp] = useState(null);
  const [totpPage, setTotpPage] = useState(1);
  const [mailText, setMailText] = useState("");
  const [mailPreview, setMailPreview] = useState(null);
  const [mailSelected, setMailSelected] = useState(() => new Set());
  const [editingMailbox, setEditingMailbox] = useState(null);
  const [mailPage, setMailPage] = useState(1);
  const [activeMailbox, setActiveMailbox] = useState(null);
  const [messages, setMessages] = useState([]);
  const [messagePage, setMessagePage] = useState(1);
  const [mailLoading, setMailLoading] = useState(false);
  const refreshLock = useRef(false);
  const totpBundle = useMemo(() => splitOAuthLoginBundle(totpInput), [totpInput]);
  const totpSource = totpBundle.secret || (!totpBundle.bundled ? totpInput.trim() : "");

  const loadToolbox = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const result = await api("/api/toolbox/state");
      const nextState = {
        totpItems: result.totpItems || [],
        mailAccounts: result.mailAccounts || [],
      };
      setState(nextState);
      setActiveMailbox((current) => current ? nextState.mailAccounts.find((item) => item.id === current.id) || null : null);
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadToolbox().catch((error) => notify(error.message, "error"));
  }, [loadToolbox, notify]);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const secondsLeft = (validUntil) => {
    const milliseconds = new Date(validUntil || 0).getTime() - clock;
    return Math.max(0, Math.ceil(milliseconds / 1000));
  };

  useEffect(() => {
    if (
      tool !== "totp" ||
      refreshLock.current ||
      !(state.totpItems || []).some(
        (item) => item.validUntil && secondsLeft(item.validUntil) === 0,
      )
    )
      return;
    refreshLock.current = true;
    loadToolbox(true)
      .catch(() => {})
      .finally(() => {
        refreshLock.current = false;
      });
  }, [clock, tool, state.totpItems, loadToolbox]);

  const totpItems = state.totpItems || [];
  const totpPageCount = Math.max(1, Math.ceil(totpItems.length / PAGE_SIZE));
  const safeTotpPage = Math.min(totpPage, totpPageCount);
  const visibleTotpItems = totpItems.slice((safeTotpPage - 1) * PAGE_SIZE, safeTotpPage * PAGE_SIZE);
  const mailAccounts = state.mailAccounts || [];
  const mailPageCount = Math.max(1, Math.ceil(mailAccounts.length / PAGE_SIZE));
  const safeMailPage = Math.min(mailPage, mailPageCount);
  const visibleMailAccounts = mailAccounts.slice((safeMailPage - 1) * PAGE_SIZE, safeMailPage * PAGE_SIZE);
  const messagePageCount = Math.max(1, Math.ceil(messages.length / MESSAGE_PAGE_SIZE));
  const safeMessagePage = Math.min(messagePage, messagePageCount);
  const visibleMessages = messages.slice((safeMessagePage - 1) * MESSAGE_PAGE_SIZE, safeMessagePage * MESSAGE_PAGE_SIZE);

  useEffect(() => { setTotpPage((page) => Math.min(page, totpPageCount)); }, [totpPageCount]);
  useEffect(() => { setMailPage((page) => Math.min(page, mailPageCount)); }, [mailPageCount]);

  const generateTotp = async () => {
    if (!totpSource) return;
    setBusy(true);
    try {
      const result = await api("/api/toolbox/totp/generate", {
        method: "POST",
        body: JSON.stringify({
          input: totpSource,
          label: totpLabel || totpBundle.account,
          save: saveTotp,
        }),
      });
      setTotpResult(result.result);
      if (saveTotp) {
        setTotpInput("");
        setTotpLabel("");
        setSaveTotp(false);
        await loadToolbox(true);
        notify("验证码密钥已加密保存");
      }
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const saveTotpEdit = async () => {
    if (!editingTotp) return;
    setBusy(true);
    try {
      await api("/api/toolbox/totp/generate", {
        method: "POST",
        body: JSON.stringify({
          input: JSON.stringify({
            _toolboxEdit: true,
            id: editingTotp.id,
            source: editingTotp.source.trim() || undefined,
          }),
          label: editingTotp.label,
          save: true,
        }),
      });
      setEditingTotp(null);
      await loadToolbox(true);
      notify("验证码项目已更新");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const removeTotp = async (item) => {
    const approved = await confirm({
      title: "删除这个验证码密钥？",
      message: item.label || item.account || "未命名验证码",
      detail: "删除后无法从本软件恢复，请确认你仍保留原始密钥或恢复码。",
      confirmLabel: "删除",
      danger: true,
    });
    if (!approved) return;
    try {
      await api(`/api/toolbox/totp/${encodeURIComponent(item.id)}`, {
        method: "DELETE",
      });
      if (editingTotp?.id === item.id) setEditingTotp(null);
      await loadToolbox(true);
      notify("验证码密钥已删除");
    } catch (error) {
      notify(error.message, "error");
    }
  };

  const previewMail = async () => {
    if (!mailText.trim()) return;
    setBusy(true);
    try {
      const result = await api("/api/toolbox/mail/preview", {
        method: "POST",
        body: JSON.stringify({ text: mailText }),
      });
      setMailPreview(result.preview);
      setMailSelected(
        new Set(
          (result.preview?.items || [])
            .filter((item) => item.valid && !item.duplicateInBatch)
            .map((item) => item.index),
        ),
      );
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const importMail = async () => {
    if (!mailSelected.size) return;
    setBusy(true);
    try {
      const result = await api("/api/toolbox/mail/import", {
        method: "POST",
        body: JSON.stringify({
          text: mailText,
          selectedIndices: [...mailSelected],
        }),
      });
      const imported = Number(result.imported || 0);
      setMailText("");
      setMailPreview(null);
      setMailSelected(new Set());
      await loadToolbox(true);
      notify(`已安全挂载 ${imported} 个邮箱`);
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const beginMailboxEdit = (account) => setEditingMailbox({
    id: account.id,
    label: account.label || "",
    email: account.email || "",
    provider: account.provider || "custom",
    imap_host: account.imapHost || "",
    imap_port: account.imapPort || 993,
    security: account.security || "ssl",
    auth_method: account.authMode || "password",
    mailbox: account.mailbox || "INBOX",
    password: "",
    access_token: "",
  });

  const saveMailboxEdit = async () => {
    if (!editingMailbox) return;
    const payload = { ...editingMailbox };
    if (!payload.password) delete payload.password;
    if (!payload.access_token) delete payload.access_token;
    setBusy(true);
    try {
      await api("/api/toolbox/mail/import", {
        method: "POST",
        body: JSON.stringify({ text: JSON.stringify(payload), selectedIndices: [0] }),
      });
      setEditingMailbox(null);
      await loadToolbox(true);
      notify("邮箱设置已加密保存");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const openMailbox = async (account) => {
    setActiveMailbox(account);
    setMessages([]);
    setMessagePage(1);
    setMailLoading(true);
    try {
      const result = await api(
        `/api/toolbox/mail/${encodeURIComponent(account.id)}/messages`,
        { method: "POST", body: JSON.stringify({ limit: 30 }) },
      );
      setMessages(result.messages || []);
      notify("邮箱健康检查通过");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      await loadToolbox(true).catch(() => {});
      setMailLoading(false);
    }
  };

  const removeMailbox = async (account) => {
    const approved = await confirm({
      title: "移除这个邮箱？",
      message: account.email,
      detail: "只会删除本机加密凭据，不会删除邮箱中的任何邮件。",
      confirmLabel: "移除",
      danger: true,
    });
    if (!approved) return;
    try {
      await api(`/api/toolbox/mail/${encodeURIComponent(account.id)}`, {
        method: "DELETE",
      });
      if (activeMailbox?.id === account.id) {
        setActiveMailbox(null);
        setMessages([]);
      }
      if (editingMailbox?.id === account.id) setEditingMailbox(null);
      await loadToolbox(true);
      notify("邮箱挂载已移除");
    } catch (error) {
      notify(error.message, "error");
    }
  };

  const activateTool = (nextTool) => {
    if (nextTool === "skills") setSkillsOpened(true);
    if (nextTool === "radar") setRadarOpened(true);
    setTool(nextTool);
  };

  const moveToolTabFocus = (event) => {
    const tabs = ["totp", "mail", "skills", "radar"];
    const direction = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!direction) return;
    event.preventDefault();
    const nextTool = tabs[(tabs.indexOf(tool) + direction + tabs.length) % tabs.length];
    activateTool(nextTool);
    window.requestAnimationFrame(() =>
      document.getElementById(`toolbox-tab-${nextTool}`)?.focus(),
    );
  };

  const healthCopy = (account) => {
    const health = account.credentialStatus?.health || {};
    if (health.status === "healthy") return `健康 · ${health.messageCount || 0} 封 · ${formatDateTime(health.checkedAt)}`;
    if (health.status === "auth_error") return `登录已失效 · ${formatDateTime(health.checkedAt)}`;
    if (health.status === "connection_error") return `网络暂不可达 · ${formatDateTime(health.checkedAt)}`;
    if (health.status === "mailbox_error") return `收件箱不可用 · ${formatDateTime(health.checkedAt)}`;
    if (health.status === "error") return `检查失败 · ${formatDateTime(health.checkedAt)}`;
    return "等待首次健康检查";
  };

  return (
    <section className="page toolbox-page">
      <header className="page-header">
        <div>
          <span className="eyebrow">LOCAL TOOLBOX</span>
          <h1>工具箱</h1>
          <p>集中管理本地安全工具、只读邮箱、Codex 技能与雷达；资源仅在首次打开时读取。</p>
        </div>
      </header>

      <div className="toolbox-tabs" role="tablist" aria-label="工具箱功能">
        <button
          id="toolbox-tab-totp"
          role="tab"
          aria-selected={tool === "totp"}
          aria-controls="toolbox-panel-totp"
          tabIndex={tool === "totp" ? 0 : -1}
          className={tool === "totp" ? "active" : ""}
          onClick={() => activateTool("totp")}
          onKeyDown={moveToolTabFocus}
        >
          <KeyRound size={18} />
          2FA 验证码
          <small>离线 TOTP</small>
        </button>
        <button
          id="toolbox-tab-mail"
          role="tab"
          aria-selected={tool === "mail"}
          aria-controls="toolbox-panel-mail"
          tabIndex={tool === "mail" ? 0 : -1}
          className={tool === "mail" ? "active" : ""}
          onClick={() => activateTool("mail")}
          onKeyDown={moveToolTabFocus}
        >
          <Mail size={18} />
          便捷邮箱
          <small>只读收件</small>
        </button>
        <button
          id="toolbox-tab-skills"
          role="tab"
          aria-selected={tool === "skills"}
          aria-controls="toolbox-panel-skills"
          tabIndex={tool === "skills" ? 0 : -1}
          className={tool === "skills" ? "active" : ""}
          onClick={() => activateTool("skills")}
          onKeyDown={moveToolTabFocus}
        >
          <Sparkles size={18} />
          技能与插件
          <small>安装与管理</small>
        </button>
        <button
          id="toolbox-tab-radar"
          role="tab"
          aria-selected={tool === "radar"}
          aria-controls="toolbox-panel-radar"
          tabIndex={tool === "radar" ? 0 : -1}
          className={tool === "radar" ? "active" : ""}
          onClick={() => activateTool("radar")}
          onKeyDown={moveToolTabFocus}
        >
          <CircleGauge size={18} />
          Codex 雷达
          <small>手动更新情报</small>
        </button>
      </div>

      {tool === "totp" && (
        <div id="toolbox-panel-totp" role="tabpanel" aria-labelledby="toolbox-tab-totp" className="toolbox-grid">
          <section className="tool-card">
            <div className="tool-card-heading">
              <div>
                <span>即时生成</span>
                <h2>粘贴 2FA 密钥</h2>
              </div>
              <KeyRound size={21} />
            </div>
            <label className="field">
              <span>2FA 密钥、otpauth:// 或账号组合</span>
              <textarea
                className="tool-secret-input"
                value={totpInput}
                onChange={(event) => setTotpInput(event.target.value)}
                placeholder={"JBSWY3DPEHPK3PXP\n账号----2FA\n账号----密码----2FA"}
                spellCheck={false}
                autoComplete="off"
              />
            </label>
            {totpBundle.bundled && (
              <div className="oauth-login-parts toolbox-login-parts" aria-label="已识别的登录信息">
                <button type="button" onClick={() => copyText(totpBundle.account).then(() => notify("账号已复制"))} title={totpBundle.account}>
                  <span>账号</span><strong>{totpBundle.account}</strong><Copy size={14} />
                </button>
                {totpBundle.password && <button type="button" onClick={() => copyText(totpBundle.password).then(() => notify("密码已复制"))} title="点击复制密码">
                  <span>密码</span><strong>{"•".repeat(Math.min(12, Math.max(6, totpBundle.password.length)))}</strong><Copy size={14} />
                </button>}
              </div>
            )}
            {totpBundle.bundled && !totpBundle.secret && (
              <p className="tool-helper warning">已拆分账号信息，但末尾内容不像 Base32 / otpauth 2FA 密钥，请检查分隔顺序。</p>
            )}
            <label className="field">
              <span>备注（可选）</span>
              <input
                value={totpLabel}
                onChange={(event) => setTotpLabel(event.target.value)}
                placeholder="例如：OpenAI 工作账号"
              />
            </label>
            <div className="tool-save-row">
              <div>
                <strong>保存到本机保险箱</strong>
                <small>使用当前 Windows 用户 DPAPI 加密；默认只即时生成，不保存。</small>
              </div>
              <Switch checked={saveTotp} onChange={setSaveTotp} ariaLabel="保存 2FA 密钥" />
            </div>
            <button className="button primary full" onClick={generateTotp} disabled={busy || !totpSource}>
              {busy ? <Loader2 className="spin" size={17} /> : <Zap size={17} />}
              生成一次性验证码
            </button>
            {totpResult && (
              <div className="totp-result">
                <div>
                  <span>{totpResult.label || totpResult.account || "当前验证码"}</span>
                  <strong>{totpResult.code}</strong>
                  <small>剩余 {secondsLeft(totpResult.validUntil)} 秒 · {totpResult.algorithm || "SHA1"}</small>
                </div>
                <button onClick={() => copyText(totpResult.code).then(() => notify("验证码已复制"))}>
                  <Copy size={18} />
                  复制
                </button>
              </div>
            )}
          </section>

          <section className="tool-card">
            <div className="tool-card-heading">
              <div>
                <span>保险箱</span>
                <h2>已保存的验证码</h2>
              </div>
              <button className="icon-plain" onClick={() => loadToolbox(true)} disabled={loading}>
                <RefreshCw size={18} />
              </button>
            </div>
            <div className="saved-totp-list">
              {visibleTotpItems.map((item) => editingTotp?.id === item.id ? (
                <article key={item.id} className="tool-inline-editor">
                  <label className="field"><span>备注</span><input value={editingTotp.label} onChange={(event) => setEditingTotp((current) => ({ ...current, label: event.target.value }))} /></label>
                  <label className="field"><span>替换密钥（留空则保留）</span><input value={editingTotp.source} onChange={(event) => setEditingTotp((current) => ({ ...current, source: event.target.value }))} autoComplete="off" spellCheck={false} /></label>
                  <div className="tool-inline-actions"><button className="button secondary compact" onClick={() => setEditingTotp(null)}>取消</button><button className="button primary compact" disabled={busy} onClick={saveTotpEdit}><Save size={14} />保存</button></div>
                </article>
              ) : (
                <article key={item.id}>
                  <div><span>{item.issuer || "TOTP"}</span><strong>{item.label || item.account || "未命名验证码"}</strong><small>剩余 {secondsLeft(item.validUntil)} 秒</small></div>
                  <button className="totp-code" onClick={() => copyText(item.code).then(() => notify("验证码已复制"))}>{item.code}<Copy size={15} /></button>
                  <div className="tool-row-actions"><IconButton title="编辑" onClick={() => setEditingTotp({ id: item.id, label: item.label || "", source: "" })}><Pencil size={16} /></IconButton><IconButton title="删除" onClick={() => removeTotp(item)}><Trash2 size={16} /></IconButton></div>
                </article>
              ))}
              {!loading && !totpItems.length && (
                <div className="tool-empty">尚未保存验证码密钥。你也可以只在左侧即时生成。</div>
              )}
            </div>
            <ToolPagination page={safeTotpPage} pageCount={totpPageCount} onChange={setTotpPage} label="验证码列表分页" />
          </section>
        </div>
      )}

      {tool === "mail" && (
        <div id="toolbox-panel-mail" role="tabpanel" aria-labelledby="toolbox-tab-mail" className="mail-tool-layout">
          <section className="tool-card mail-import-card">
            <div className="tool-card-heading">
              <div>
                <span>批量挂载</span>
                <h2>导入邮箱凭据</h2>
              </div>
              <Upload size={21} />
            </div>
            <label className="field">
              <span>JSON / JSONL / 分隔文本</span>
              <textarea
                className="mail-import-input"
                value={mailText}
                onChange={(event) => {
                  setMailText(event.target.value);
                  setMailPreview(null);
                }}
                placeholder={'支持字段别名与混合格式：\nemail----password----refresh_token----client_id\n{"email":"...","password":"...","imap_host":"..."}\n\n兼容应用密码、Access Token、Refresh Token。'}
                spellCheck={false}
                autoComplete="off"
              />
            </label>
            <p className="tool-helper">
              支持 Outlook / Microsoft 365、Gmail 和标准 IMAP。首次读取前会先预览识别结果；邮件默认只读，不会自动标记已读。
            </p>
            <button className="button primary full" onClick={previewMail} disabled={busy || !mailText.trim()}>
              {busy ? <Loader2 className="spin" size={17} /> : <Search size={17} />}
              识别并预览
            </button>
            {mailPreview && (
              <div className="mail-preview">
                <div className="mail-preview-heading">
                  <strong>识别到 {mailPreview.total || mailPreview.items?.length || 0} 项</strong>
                  <span>{mailSelected.size} 已选择</span>
                </div>
                {(mailPreview.items || []).map((item) => (
                  <button
                    key={item.index}
                    className={cx("mail-preview-item", item.valid && mailSelected.has(item.index) && "selected", !item.valid && "invalid")}
                    disabled={!item.valid}
                    onClick={() =>
                      item.valid &&
                      setMailSelected((current) => {
                        const next = new Set(current);
                        next.has(item.index) ? next.delete(item.index) : next.add(item.index);
                        return next;
                      })
                    }
                  >
                    {item.valid && mailSelected.has(item.index) ? <CheckSquare size={17} /> : <Square size={17} />}
                    <span>
                      <strong>{item.email || `第 ${item.index + 1} 项`}</strong>
                      <small>{item.error || `${item.provider || "标准 IMAP"} · ${item.authMode || "自动识别"}`}</small>
                    </span>
                  </button>
                ))}
                <button className="button primary full" onClick={importMail} disabled={busy || !mailSelected.size}>
                  加密挂载 {mailSelected.size} 个邮箱
                </button>
              </div>
            )}
          </section>

          <section className="tool-card mailbox-card">
            <div className="tool-card-heading">
              <div>
                <span>邮箱列表</span>
                <h2>验证码收件箱</h2>
              </div>
              <Mail size={21} />
            </div>
            <div className="mail-account-list">
              {visibleMailAccounts.map((account) => editingMailbox?.id === account.id ? (
                <article key={account.id} className="mail-account-editor">
                  <div className="mail-edit-grid">
                    <label className="field"><span>显示名称</span><input value={editingMailbox.label} onChange={(event) => setEditingMailbox((current) => ({ ...current, label: event.target.value }))} /></label>
                    <label className="field"><span>邮箱地址</span><input value={editingMailbox.email} onChange={(event) => setEditingMailbox((current) => ({ ...current, email: event.target.value }))} /></label>
                    <label className="field"><span>IMAP 主机</span><input value={editingMailbox.imap_host} onChange={(event) => setEditingMailbox((current) => ({ ...current, imap_host: event.target.value }))} /></label>
                    <label className="field"><span>端口</span><input type="number" value={editingMailbox.imap_port} onChange={(event) => setEditingMailbox((current) => ({ ...current, imap_port: Number(event.target.value) }))} /></label>
                    <label className="field"><span>替换密码（可选）</span><input type="password" value={editingMailbox.password} onChange={(event) => setEditingMailbox((current) => ({ ...current, password: event.target.value }))} autoComplete="new-password" /></label>
                    <label className="field"><span>替换 Access Token（可选）</span><input type="password" value={editingMailbox.access_token} onChange={(event) => setEditingMailbox((current) => ({ ...current, access_token: event.target.value }))} autoComplete="off" /></label>
                  </div>
                  <div className="tool-inline-actions"><button className="button secondary compact" onClick={() => setEditingMailbox(null)}>取消</button><button className="button primary compact" disabled={busy} onClick={saveMailboxEdit}><Save size={14} />保存</button></div>
                </article>
              ) : (
                <article key={account.id} className={activeMailbox?.id === account.id ? "active" : ""}>
                  <button className="mail-account-main" onClick={() => openMailbox(account)}>
                    <span className="mail-provider-mark"><Mail size={17} /></span>
                    <span><strong>{account.label || account.email}</strong><small>{account.email} · {account.provider || "IMAP"} · {account.authMode || "密码"}</small><em className={cx("mail-health", account.credentialStatus?.health?.status || "unknown")}>{healthCopy(account)}</em></span>
                  </button>
                  <div className="tool-row-actions"><IconButton title="手动检查" onClick={() => openMailbox(account)}><RefreshCw className={mailLoading && activeMailbox?.id === account.id ? "spin" : ""} size={16} /></IconButton><IconButton title="编辑邮箱" onClick={() => beginMailboxEdit(account)}><Pencil size={16} /></IconButton><IconButton title="移除邮箱" onClick={() => removeMailbox(account)}><Trash2 size={16} /></IconButton></div>
                </article>
              ))}
              {!loading && !mailAccounts.length && (
                <div className="tool-empty">还没有挂载邮箱。导入后凭据仅以本机加密形式保存。</div>
              )}
            </div>
            <ToolPagination page={safeMailPage} pageCount={mailPageCount} onChange={setMailPage} label="邮箱列表分页" />
          </section>

          <section className="tool-card mail-messages-card">
            <div className="tool-card-heading">
              <div>
                <span>只读收件</span>
                <h2>{activeMailbox?.email || "选择一个邮箱"}</h2>
              </div>
              {activeMailbox && (
                <button className="icon-plain" onClick={() => openMailbox(activeMailbox)} disabled={mailLoading}>
                  <RefreshCw className={mailLoading ? "spin" : ""} size={18} />
                </button>
              )}
            </div>
            <div className="mail-message-list">
              {visibleMessages.map((message) => (
                <article key={message.id}>
                  <header>
                    <div>
                      <strong>{message.subject || "（无主题）"}</strong>
                      <small>{message.from || "未知发件人"} · {formatDateTime(message.receivedAt)}</small>
                    </div>
                    {(message.codes || []).length > 0 && (
                      <div className="mail-codes">
                        {message.codes.map((code) => (
                          <button key={code} onClick={() => copyText(code).then(() => notify("验证码已复制"))}>
                            {code}<Copy size={14} />
                          </button>
                        ))}
                      </div>
                    )}
                  </header>
                  <p>{message.preview || "邮件没有可显示的纯文本内容。"}</p>
                </article>
              ))}
              {mailLoading && <div className="tool-empty"><Loader2 className="spin" size={18} /> 正在安全读取最近邮件…</div>}
              {!mailLoading && activeMailbox && !messages.length && <div className="tool-empty">最近没有可显示的邮件。</div>}
              {!activeMailbox && <div className="tool-empty">点击上方邮箱后手动读取；工具箱不会在后台频繁轮询邮箱。</div>}
            </div>
            <ToolPagination page={safeMessagePage} pageCount={messagePageCount} onChange={setMessagePage} label="邮件列表分页" />
          </section>
        </div>
      )}
      {skillsOpened && (
        <div id="toolbox-panel-skills" role="tabpanel" aria-labelledby="toolbox-tab-skills" className={cx("toolbox-skills", tool !== "skills" && "is-hidden")}>
          <SkillsView notify={notify} confirm={confirm} embedded />
        </div>
      )}
      {radarOpened && (
        <div id="toolbox-panel-radar" role="tabpanel" aria-labelledby="toolbox-tab-radar" className={cx("toolbox-radar", tool !== "radar" && "is-hidden")}>
          <RadarView data={data} updateData={updateData} notify={notify} />
        </div>
      )}
    </section>
  );
}

function radarField(source, keys, fallback = "待同步") {
  for (const key of keys) {
    const value = source?.[key];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return fallback;
}

function radarText(value, fallback = "待同步") {
  if (value === undefined || value === null || value === "") return fallback;
  if (Array.isArray(value)) {
    return value.map((item) => radarText(item, "")).filter(Boolean).join(" · ") || fallback;
  }
  if (typeof value === "object") {
    return radarText(
      value.label ?? value.title ?? value.name ?? value.value ?? value.text,
      fallback,
    );
  }
  return String(value);
}

function radarDate(value) {
  if (!value || value === "待同步") return "待同步";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? radarText(value) : formatDateTime(value);
}

function radarPercent(value, fallback = "待同步") {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  const percent = number <= 1 ? number * 100 : number;
  return `${Math.max(0, Math.min(100, percent)).toFixed(percent % 1 ? 1 : 0)}%`;
}

function radarSourceChinese(value) {
  const text = radarText(value, "推算");
  const normalized = text.trim().toLowerCase();
  return {
    "distributed radar": "分布式雷达",
    distributed_community_runs: "分布式社区实测",
    estimated: "推算",
    estimate: "推算",
    measured: "实测",
    public: "公开数据",
  }[normalized] || text;
}

function radarTone(item) {
  const value = `${item?.family || ""} ${item?.model || ""}`.toLowerCase();
  if (value.includes("sol")) return "sol";
  if (value.includes("terra")) return "terra";
  if (value.includes("luna")) return "luna";
  if (value.includes("deepseek")) return "deepseek";
  if (value.includes("5.5")) return "gpt55";
  return "default";
}

function radarFamilyLabel(value) {
  const text = radarText(value, "Codex");
  const normalized = text.trim().toLowerCase();
  const labels = [
    ["dsh-deepseek-v4-flash", "DSH Flash"],
    ["dsh-deepseek-v4-pro", "DSH Pro"],
    ["deepseek-v4-flash", "DSV4 Flash"],
    ["deepseek-v4-pro", "DSV4 Pro"],
    ["gpt-5.5", "5.5"],
    ["deepseek", "DeepSeek"],
    ["terra", "Terra"],
    ["luna", "Luna"],
    ["sol", "Sol"],
    ["grok", "Grok"],
    ["k3", "K3"],
    ["glm", "GLM"],
  ];
  for (const [key, label] of labels) {
    if (normalized === key || normalized.includes(key)) return label;
  }
  return text;
}

const radarEffortOrder = { ultra: 0, max: 1, xhigh: 2, high: 3, medium: 4, low: 5 };
const radarEffortColumn = { ultra: 1, max: 2, xhigh: 3, high: 4, medium: 5, low: 6, off: 6 };
const radarFamilyOrder = { sol: 0, terra: 1, luna: 2, gpt55: 3, deepseek: 4, default: 9 };

function radarIq(value) {
  const number = Number(value);
  return Number.isFinite(number)
    ? new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(Math.round(number))
    : radarText(value);
}

function RadarTrend({ points }) {
  const values = points
    .map((point) => {
      const value = typeof point === "number" ? point : radarField(point, ["value", "usage", "estimate", "remainingPercent", "quota", "score"], null);
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    })
    .filter((value) => value !== null)
    .slice(-28);
  if (values.length < 2) return null;
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const range = maximum - minimum || 1;
  const coordinates = values.map((value, index) => {
    const x = (index / (values.length - 1)) * 100;
    const y = 31 - ((value - minimum) / range) * 25;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });
  return (
    <div className="radar-trend-chart" aria-label={`额度趋势，最近 ${values.length} 个数据点`}>
      <svg viewBox="0 0 100 36" preserveAspectRatio="none" role="img">
        <title>额度趋势</title>
        <polyline points={coordinates.join(" ")} />
        {coordinates.map((coordinate, index) => {
          const [cx, cy] = coordinate.split(",");
          return <circle key={`${coordinate}-${index}`} cx={cx} cy={cy} r="1.65" />;
        })}
      </svg>
      <span>近 {values.length} 次估算趋势</span>
    </div>
  );
}

function RadarView({ data, updateData, notify }) {
  const [payload, setPayload] = useState(() => radarResourceCache.payload);
  const [loading, setLoading] = useState(() => !radarResourceCache.loaded);
  const [initialError, setInitialError] = useState(() => radarResourceCache.error);
  const [sectionErrors, setSectionErrors] = useState({});
  const [sectionMessages, setSectionMessages] = useState({});
  const [refreshing, setRefreshing] = useState({});
  const monitoringEnabled = data?.settings?.appBehavior?.radarMonitoring === true;

  const mergePayload = useCallback((nextPayload) => {
    setPayload((current) => {
      const merged = {
        ...(current || {}),
        ...(nextPayload || {}),
        intelligence: nextPayload?.intelligence ?? current?.intelligence ?? null,
        quota: nextPayload?.quota ?? current?.quota ?? null,
        reset: nextPayload?.reset ?? current?.reset ?? null,
        meta: { ...(current?.meta || {}), ...(nextPayload?.meta || {}) },
      };
      radarResourceCache = { loaded: true, payload: merged, error: "" };
      return merged;
    });
  }, []);

  useEffect(() => {
    let active = true;
    const syncStartupRadar = () => {
      if (!active) return;
      setPayload(radarResourceCache.payload);
      setInitialError(radarResourceCache.error);
      setLoading(!radarResourceCache.loaded);
    };
    syncStartupRadar();
    if (!radarResourceCache.loaded) {
      refreshStartupResourcesOnce().finally(syncStartupRadar);
    }
    return () => {
      active = false;
    };
  }, []);

  const refreshSection = async (section) => {
    setRefreshing((current) => ({ ...current, [section]: true }));
    setSectionErrors((current) => ({ ...current, [section]: "" }));
    setSectionMessages((current) => ({ ...current, [section]: null }));
    try {
      const result = await api("/api/radar/refresh", {
        method: "POST",
        body: JSON.stringify({ section }),
      });
      mergePayload(result);
      const refreshed = result?.[section] || {};
      const refreshMeta = refreshed?.meta || {};
      const suppressed = Boolean(refreshed?.refreshSuppressed ?? refreshMeta?.refreshSuppressed);
      const nextRefreshAt = radarDate(
        refreshed?.nextWeeklyRefreshAt
          ?? refreshed?.nextRefreshAt
          ?? refreshed?.nextAllowedAt
          ?? refreshMeta?.nextAllowedAt,
      );
      setSectionMessages((current) => ({
        ...current,
        [section]: suppressed
          ? {
              tone: "warning",
              text: nextRefreshAt === "待同步"
                ? "本次刷新已按数据源节奏抑制，当前继续展示最近一次有效数据。"
                : `本次刷新已按数据源节奏抑制，下一次建议刷新：${nextRefreshAt}。`,
            }
          : { tone: "success", text: "手动刷新已完成，当前展示最新可用数据。" },
      }));
    } catch (error) {
      setSectionErrors((current) => ({ ...current, [section]: error.message }));
    } finally {
      setRefreshing((current) => ({ ...current, [section]: false }));
    }
  };
  useEffect(() => {
    if (!monitoringEnabled) return undefined;
    let stopped = false, inFlight = false;
    const sync = async () => {
      if (stopped || inFlight || document.visibilityState !== "visible") return;
      inFlight = true;
      try {
        const result = await api("/api/radar/reset-status");
        if (!stopped) mergePayload(result);
      } catch { /* The monitor result retains its own error state. */ }
      finally { inFlight = false; }
    };
    const timer = window.setInterval(sync, 30000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [monitoringEnabled, mergePayload]);
  const checkResetNow = async () => {
    setRefreshing(current => ({...current, check: true}));
    setSectionErrors(current => ({...current, reset: ""}));
    try {
      const result = await api("/api/radar/check", {method:"POST",body:"{}"});
      mergePayload(result);
      if (result.result?.success === false) setSectionErrors(current => ({...current,reset:result.result?.state?.lastError || "本次检查未完成"}));
    } catch (error) { setSectionErrors(current => ({...current,reset:error.message})); }
    finally { setRefreshing(current => ({...current,check:false})); }
  };
  const toggleResetMonitoring = async enabled => {
    setRefreshing(current => ({...current, monitor:true}));
    try {
      const result = await api("/api/app-behavior", {method:"POST",body:JSON.stringify({radarMonitoring:enabled})});
      updateData(current => ({...current,settings:{...current.settings,appBehavior:result.behavior}}));
      notify(enabled ? "后台预警已开启，管理器运行时在北京时间整点检查" : "后台预警已暂停，仍可手动检查");
      if (enabled) await checkResetNow();
    } catch (error) { notify(error.message,"error"); }
    finally { setRefreshing(current => ({...current,monitor:false})); }
  };

  const intelligence = payload?.intelligence || {};
  const quota = payload?.quota || {};
  const reset = payload?.reset || {};
  const intelligenceItems = firstLongestArray(intelligence, ["items", "points", "configurations", "cells"]);
  const intelligenceGroups = intelligenceItems.reduce((groups, item) => {
    const family = radarText(radarField(item, ["family", "modelFamily", "series", "provider"], "Codex"));
    const current = groups.get(family) || [];
    current.push(item);
    groups.set(family, current);
    return groups;
  }, new Map());
  const intelligenceFamilies = [...intelligenceGroups.entries()]
    .map(([family, items]) => {
      const familyKey = radarTone({ family });
      return {
        family,
        familyKey,
        familyOrder: Number.isFinite(Number(items[0]?.familyOrder)) ? Number(items[0].familyOrder) : null,
        items: [...items].sort((left, right) => {
          const leftEffort = String(left?.effort || "").toLowerCase();
          const rightEffort = String(right?.effort || "").toLowerCase();
          return (radarEffortOrder[leftEffort] ?? 99) - (radarEffortOrder[rightEffort] ?? 99);
        }),
      };
    })
    .sort((left, right) => {
      if (left.familyOrder !== null || right.familyOrder !== null) {
        return (left.familyOrder ?? 99) - (right.familyOrder ?? 99);
      }
      return (radarFamilyOrder[left.familyKey] ?? 9) - (radarFamilyOrder[right.familyKey] ?? 9);
    });
  const quotaTiers = firstArray(quota, ["tiers", "plans", "items"]);
  const quotaTrendPoints = firstArray(quota, ["history", "trendPoints", "points"]);

  const sectionMeta = (section, data) => {
    const meta = payload?.meta || {};
    return {
      ...(typeof meta?.[section] === "object" ? meta[section] : {}),
      ...(data?.meta || {}),
    };
  };

  const sectionState = (section, data, defaultSource) => {
    const meta = sectionMeta(section, data);
    const remoteError = radarText(radarField(meta, ["error", "message", "lastError"], ""), "");
    const stale = Boolean(meta.stale ?? data?.stale);
    return {
      stale,
      error: sectionErrors[section] || remoteError,
      source: radarText(radarField(data, ["source", "sourceLabel"], radarField(meta, ["source", "sourceLabel", "provenance"], defaultSource))),
      confidence: radarText(radarField(data, ["confidence", "confidenceLabel"], radarField(meta, ["confidence", "confidenceLabel", "certainty"], "未标注"))),
      updatedAt: radarDate(radarField(data, ["updatedAt", "fetchedAt", "sourceUpdatedAt"], radarField(meta, ["updatedAt", "fetchedAt", "checkedAt"], null))),
      refreshSuppressed: Boolean(radarField(data, ["refreshSuppressed"], radarField(meta, ["refreshSuppressed"], false))),
      nextRefreshAt: radarDate(radarField(data, ["nextWeeklyRefreshAt", "nextRefreshAt", "nextAllowedAt"], radarField(meta, ["nextAllowedAt"], null))),
      feedback: sectionMessages[section] || null,
    };
  };

  const intelligenceState = sectionState("intelligence", intelligence, "社区公开基准与经验报告");
  const quotaState = sectionState("quota", quota, "本地账号状态与订阅信息");
  const resetState = sectionState("reset", reset, "OpenAI 公开活动与本地账号状态");
  const renderState = (state) => (
    <footer className="radar-card-footer">
      <span>来源：{state.source}</span>
      <span>置信度：{state.confidence}</span>
      <time>更新：{state.updatedAt}</time>
      <a href="https://codexradar.com/" target="_blank" rel="noreferrer">数据来自 Codex 雷达 codexradar.com</a>
    </footer>
  );
  const renderNotice = (state) => {
    if (!state.stale && !state.error && !state.refreshSuppressed && !state.feedback) return null;
    const suppressedText = state.nextRefreshAt === "待同步"
      ? "本次刷新已按数据源节奏抑制，当前展示最近一次有效数据。"
      : `本次刷新已按数据源节奏抑制，下一次建议刷新：${state.nextRefreshAt}。`;
    const message = state.error
      ? `刷新失败：${state.error}；已保留上次可用数据。`
      : state.refreshSuppressed
        ? suppressedText
        : state.stale
          ? "远端数据已标记为过期，当前展示上次可用结果。"
          : state.feedback?.text;
    return (
      <div className={cx("radar-state", state.error && "error", !state.error && (state.stale || state.refreshSuppressed || state.feedback?.tone === "warning") && "stale", !state.error && !state.stale && !state.refreshSuppressed && state.feedback?.tone === "success" && "success")} role="status" aria-live="polite">
        {state.feedback?.tone === "success" && !state.error && !state.stale && !state.refreshSuppressed ? <Check size={14} /> : <AlertTriangle size={14} />}
        <span>{message}</span>
      </div>
    );
  };

  return (
    <section className="radar-page" aria-label="Codex 雷达">
      <header className="radar-heading">
        <div>
          <span className="eyebrow">CODEX RADAR</span>
          <h2>Codex 雷达</h2>
          <p>启动时读取一次；可按需手动刷新，也可开启重置雷达的后台预警。不调用模型或消耗 Token。</p>
        </div>
        {loading && <span className="status-pill neutral"><Loader2 className="spin" size={13} />正在读取</span>}
      </header>

      {initialError && !payload && (
        <div className="inline-notice warning radar-initial-error" role="status">
          <AlertTriangle size={16} />
          <span>首次读取失败：{initialError}。可分别使用各卡的“手动刷新”重试。</span>
        </div>
      )}

      <div className="radar-grid">
        <article className="radar-card intelligence">
          <header>
            <span className="radar-icon"><BrainCircuit size={20} /></span>
            <div><small>COMPREHENSIVE INTELLIGENCE</small><h3>综合智能</h3></div>
            <button className="icon-plain" onClick={() => refreshSection("intelligence")} disabled={refreshing.intelligence} aria-label="手动刷新综合智能雷达">
              <RefreshCw className={refreshing.intelligence ? "spin" : ""} size={16} />
            </button>
          </header>
          {renderNotice(intelligenceState)}
          <div className="radar-intelligence-meta">
            <span>
              <RefreshCw size={13} />
              数据截至 {intelligenceState.updatedAt} · 综合智能按软件工程与视觉空间两项等权合成
            </span>
            <a href="https://deng.codexradar.com/?harness=codex" target="_blank" rel="noreferrer">
              前往贡献 <ExternalLink size={12} />
            </a>
          </div>
          {intelligenceItems.length ? (
            <div className="radar-matrix" aria-label="模型与推理档位评估矩阵">
              {intelligenceFamilies.map(({ family, familyKey, items }) => (
                <section key={family} className={cx("radar-model-family", `family-${familyKey}`)} aria-label={`${radarFamilyLabel(family)} 综合智能`}>
                  <div className="radar-model-grid">
                    {items.map((item, index) => {
                      const effortKey = String(item?.effort || "").trim().toLowerCase();
                      const gridColumn = radarEffortColumn[effortKey];
                      return (
                      <article className={cx("radar-score-card", `tone-${radarTone(item)}`)} style={gridColumn ? { gridColumn } : undefined} key={item.id || `${family}-${item.model || item.name || index}`}>
                        <header>
                          <strong>{radarText(radarField(item, ["label", "name", "model", "modelName"], family)).replace(/^GPT-5\.[56]\s+/i, "")}</strong>
                          <span title="近 24 小时两项能力的有效样本合计">
                            {radarText(radarField(item, ["sampleCount", "samples", "n"], "—"))}
                          </span>
                        </header>
                        <div className="radar-score-main">
                          <strong aria-label={`综合智能 ${radarIq(radarField(item, ["iq", "score", "overallScore", "rating"]))}`}>
                            {radarIq(radarField(item, ["iq", "score", "overallScore", "rating"]))}
                          </strong>
                          <div>
                            <span>{radarText(radarField(item, ["cost", "costEstimate", "price"]))}</span>
                            <span>{radarText(radarField(item, ["duration", "latency", "elapsed"]))}</span>
                          </div>
                        </div>
                      </article>
                      );
                    })}
                  </div>
                </section>
              ))}
            </div>
          ) : (
            <dl className="radar-metrics">
              <div><dt>模型</dt><dd>{radarText(radarField(intelligence, ["model", "modelName", "recommendedModel"]))}</dd></div>
              <div><dt>推理等级</dt><dd>{radarText(radarField(intelligence, ["reasoningLevel", "reasoning", "effort"]))}</dd></div>
              <div><dt>得分</dt><dd>{radarText(radarField(intelligence, ["score", "overallScore", "rating"]))}</dd></div>
              <div><dt>效率</dt><dd>{radarText(radarField(intelligence, ["efficiency", "efficiencyScore", "throughput"]))}</dd></div>
              <div><dt>耗时</dt><dd>{radarText(radarField(intelligence, ["latency", "duration", "elapsed"]))}</dd></div>
              <div><dt>成本</dt><dd>{radarText(radarField(intelligence, ["cost", "costEstimate", "price"]))}</dd></div>
            </dl>
          )}
          <div className="radar-community-note"><Globe2 size={15} /><span>社区来源说明：仅汇总公开基准、开发者经验与可复核讨论，不代表 OpenAI 官方性能或价格承诺。</span></div>
          {renderState(intelligenceState)}
        </article>

        <article className="radar-card quota">
          <header>
            <span className="radar-icon"><Gauge size={20} /></span>
            <div><small>SUBSCRIPTION ESTIMATE</small><h3>额度雷达</h3></div>
            <button className="icon-plain" onClick={() => refreshSection("quota")} disabled={refreshing.quota} aria-label="手动刷新额度雷达">
              <RefreshCw className={refreshing.quota ? "spin" : ""} size={16} />
            </button>
          </header>
          {renderNotice(quotaState)}
          {quotaTiers.length ? (
            <div className="radar-tier-table" tabIndex="0" role="region" aria-label="订阅档位额度估算表">
              <table>
                <thead><tr><th>订阅档位</th><th>7d 额度</th><th>来源</th></tr></thead>
                <tbody>
                  {quotaTiers.slice(0, 12).map((tier, index) => (
                    <tr key={tier.id || tier.slug || tier.name || index}>
                      <th scope="row">{radarText(radarField(tier, ["label", "name", "title", "plan", "planLabel", "tier"]))}</th>
                      <td>{radarText(radarField(tier, ["estimated7d", "estimate7d", "weeklyEstimate", "usage7d", "quota"]))}</td>
                      <td>{radarSourceChinese(radarField(tier, ["sourceLabel", "basis", "source"], "推算"))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <>
              <div className="radar-quota-summary">
                <span>订阅档位</span>
                <strong>{radarText(radarField(quota, ["plan", "planLabel", "tier", "subscription", "subscriptionTier"]))}</strong>
              </div>
              <dl className="radar-metrics compact">
                <div><dt>7d 估算</dt><dd>{radarText(radarField(quota, ["estimated7d", "estimate7d", "weeklyEstimate", "usage7d"]))}</dd></div>
                <div><dt>趋势</dt><dd>{radarText(radarField(quota, ["trend", "trendLabel", "direction"]))}</dd></div>
              </dl>
            </>
          )}
          <RadarTrend points={quotaTrendPoints} />
          <div className="radar-weekly-note"><CalendarDays size={15} /><span>7d 估算每周最多更新一次，不以实时剩余额度或账单数据冒充精确值。</span></div>
          {renderState(quotaState)}
        </article>

        <ResetRadarPanel reset={reset} sectionState={resetState} refreshing={Boolean(refreshing.reset || refreshing.monitor)} checking={Boolean(refreshing.check)} onRefresh={() => refreshSection("reset")} onCheckNow={checkResetNow} onToggleMonitoring={toggleResetMonitoring} onRetryTranslation={() => refreshSection("reset")} formatDate={formatDateTime} sourceUrl="https://codexradar.com/" monitoringEnabled={monitoringEnabled} />
      </div>
    </section>
  );
}

function firstArray(payload, keys) {
  for (const key of keys) {
    if (Array.isArray(payload?.[key])) return payload[key];
  }
  return Array.isArray(payload) ? payload : [];
}

function firstLongestArray(payload, keys) {
  const initial = Array.isArray(payload) ? payload : [];
  return keys.reduce((longest, key) => {
    const candidate = payload?.[key];
    return Array.isArray(candidate) && candidate.length > longest.length ? candidate : longest;
  }, initial);
}

const skillCategoryLabels = {
  productivity: "生产力",
  communication: "沟通协作",
  creativity: "创意",
  "developer tools": "开发工具",
  development: "开发工具",
  "data & analytics": "数据与分析",
  "data and analytics": "数据与分析",
  finance: "金融",
  "business & operations": "业务与运营",
  "business and operations": "业务与运营",
  "education & research": "教育与研究",
  "education and research": "教育与研究",
  research: "研究",
  design: "设计",
  marketing: "营销",
  media: "媒体",
  utilities: "实用工具",
  utility: "实用工具",
  other: "其他",
};

function skillCategoryLabel(value) {
  const text = String(value || "").trim();
  if (!text) return "扩展";
  if (/[\u3400-\u9fff]/.test(text)) return text;
  return skillCategoryLabels[text.toLowerCase()] || text;
}

const skillScopeLabels = {
  user: "用户技能",
  builtin: "内置技能",
  system: "系统技能",
  "plugin-cache": "插件缓存",
  official: "官方扩展",
};

function skillScopeLabel(value) {
  const text = String(value || "").trim();
  if (!text) return "用户技能";
  if (/[\u3400-\u9fff]/.test(text)) return text;
  return skillScopeLabels[text.toLowerCase()] || text;
}

function skillDisplayText(item, keys, fallback = "") {
  for (const key of keys) {
    const value = String(item?.[key] || "").trim();
    if (value) return value;
  }
  return fallback;
}

function skillNameText(item, fallback) {
  return skillDisplayText(item, ["displayNameZh", "nameZh", "titleZh", "displayName", "name"], fallback);
}

function skillDescriptionText(item) {
  const localized = skillDisplayText(item, ["descriptionZh", "longDescriptionZh", "shortDescriptionZh", "summaryZh"]);
  if (localized) return localized;
  const description = skillDisplayText(item, ["description", "longDescription", "shortDescription", "summary"]);
  if (/[\u3400-\u9fff]/.test(description)) return description;
  const searchable = `${item?.name || ""} ${item?.displayName || ""} ${description}`.toLowerCase();
  const summaries = [
    [/spreadsheet|excel|csv|workbook/, "创建、编辑、分析并验证电子表格文件。"],
    [/presentation|powerpoint|pptx|slides?/, "创建、编辑并检查演示文稿。"],
    [/document|word|docx|google docs/, "创建、编辑并检查文档。"],
    [/pdf/, "读取、创建、检查并验证 PDF 文件。"],
    [/browser|chrome|webpage|web page/, "控制浏览器并完成网页查看与交互。"],
    [/image|illustration|raster|visual asset/, "生成或编辑图片与视觉素材。"],
    [/research|literature|academic|paper/, "辅助文献研究、论文阅读与学术写作。"],
    [/zotero|citation|reference manager/, "管理文献、引用与参考资料。"],
    [/ui|ux|design|styling/, "辅助界面、体验与视觉设计。"],
    [/transcrib|audio|speech/, "将音频或视频中的语音转写为文本。"],
    [/jupyter|notebook/, "创建或编辑 Jupyter Notebook。"],
    [/notion/, "整理并汇总 Notion 中的研究资料。"],
    [/data|analytics|chart|visualiz/, "进行数据分析并生成可视化。"],
    [/brand|marketing|banner/, "辅助品牌、营销与视觉内容制作。"],
    [/plugin|extension/, "安装、管理或创建 Codex 扩展。"],
    [/skill/, "创建、安装或管理 Codex 技能。"],
  ];
  const match = summaries.find(([pattern]) => pattern.test(searchable));
  return match?.[1] || `${skillCategoryLabel(item?.category)}类 Codex 扩展。`;
}

function ResourceState({ loading, error, onRetry, label = "正在读取…" }) {
  if (loading)
    return (
      <div className="resource-state loading" role="status">
        <Loader2 className="spin" size={21} />
        <span>{label}</span>
      </div>
    );
  if (!error) return null;
  return (
    <div className="resource-state error" role="alert">
      <AlertTriangle size={21} />
      <span>
        <strong>暂时无法读取</strong>
        <small>{error}</small>
      </span>
      <button className="button secondary compact" onClick={onRetry}>
        <RefreshCw size={14} />
        重试
      </button>
    </div>
  );
}

function SkillsView({ notify, confirm, embedded = false }) {
  const SKILL_PAGE_SIZE = 9;
  const [tab, setTab] = useState("installed");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [installedPayload, setInstalledPayload] = useState(null);
  const [catalogPayload, setCatalogPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [workingId, setWorkingId] = useState("");
  const load = useCallback(async (force = false) => {
    if (!force && skillsResourceCache) {
      setInstalledPayload(skillsResourceCache.installedPayload);
      setCatalogPayload(skillsResourceCache.catalogPayload);
      setError(skillsResourceCache.error || "");
      setLoading(false);
      return;
    }
    setLoading(true);
    setError("");
    const [installed, catalog] = await Promise.allSettled([
      api(force ? "/api/skills?force=1" : "/api/skills"),
      api(force ? "/api/skills/catalog?force=1" : "/api/skills/catalog"),
    ]);
    if (installed.status === "fulfilled") setInstalledPayload(installed.value);
    if (catalog.status === "fulfilled") setCatalogPayload(catalog.value);
    const failures = [installed, catalog]
      .filter((item) => item.status === "rejected")
      .map((item) => item.reason?.message)
      .filter(Boolean);
    const nextError = failures.join("；");
    setError(nextError);
    skillsResourceCache = {
      installedPayload: installed.status === "fulfilled" ? installed.value : null,
      catalogPayload: catalog.status === "fulfilled" ? catalog.value : null,
      error: nextError,
    };
    setLoading(false);
  }, []);
  useEffect(() => {
    let active = true;
    if (skillsResourceCache) load(false);
    else
      refreshStartupResourcesOnce().then(() => {
        if (active) load(false);
      });
    return () => {
      active = false;
    };
  }, [load]);
  const installed = firstLongestArray(installedPayload, ["skills", "installed", "items"]);
  const catalog = firstLongestArray(catalogPayload, ["items", "plugins", "catalog", "skills"]);
  const activeItems = tab === "installed" ? installed : catalog;
  const normalizedQuery = query.trim().toLowerCase();
  const filteredItems = activeItems.filter((item) => !normalizedQuery || [
    skillNameText(item, ""), skillDescriptionText(item), item.name, item.displayName, item.description,
    item.category, skillCategoryLabel(item.category), item.scopeLabel, item.scope, item.path,
  ].some((value) => String(value || "").toLowerCase().includes(normalizedQuery)));
  const skillPageCount = Math.max(1, Math.ceil(filteredItems.length / SKILL_PAGE_SIZE));
  const safeSkillPage = Math.min(page, skillPageCount);
  const visibleSkills = filteredItems.slice((safeSkillPage - 1) * SKILL_PAGE_SIZE, safeSkillPage * SKILL_PAGE_SIZE);
  const installedNames = new Set(
    installed.map((item) => String(skillNameText(item, item.id || "")).toLowerCase()),
  );
  useEffect(() => { setPage(1); }, [tab, query]);
  useEffect(() => { setPage((current) => Math.min(current, skillPageCount)); }, [skillPageCount]);
  const perform = async (id, action, successMessage, refreshMode = "force") => {
    setWorkingId(id);
    try {
      const response = await action();
      const warning = response?.cacheWarning || response?.result?.cacheWarning;
      notify(warning ? `${successMessage}；${warning}` : successMessage, warning ? "warning" : "success");
      if (refreshMode === "cached") {
        // Mutations update the backend inventory cache themselves.  Re-read
        // that cache without rescanning every file or refreshing the public
        // catalog; only the explicit “刷新目录” action performs a force scan.
        skillsResourceCache = null;
        await load(false);
      } else if (refreshMode === "force") {
        await load(true);
      }
    } catch (actionError) {
      notify(actionError.message, "error");
    } finally {
      setWorkingId("");
    }
  };
  const toggleSkill = (skill) =>
    perform(
      skill.id || skill.path || skill.name,
      () =>
        api("/api/skills/toggle", {
          method: "POST",
          body: JSON.stringify({
            id: skill.id,
            cwd: skill.cwd,
            enabled: skill.enabled === false,
          }),
        }),
      skill.enabled === false ? "技能已启用" : "技能已停用",
      "cached",
    );
  const deleteSkill = async (skill) => {
    const approved = await confirm({
      tone: "danger",
      title: `删除技能“${skill.name || skill.id}”？`,
      message: "技能会先移入可恢复的本地回收目录，并清理对应启停配置。",
      detail: "内置技能与插件缓存为只读，不会允许删除。",
      confirmLabel: "删除技能",
    });
    if (!approved) return;
    const id = skill.id || skill.name;
    await perform(
      id,
      () =>
        api(`/api/skills/${encodeURIComponent(id)}`, {
          method: "DELETE",
          body: JSON.stringify({
            cwd: skill.cwd,
            expectedFingerprint: skill.fingerprint,
          }),
        }),
      "技能已移入回收目录",
      "cached",
    );
  };
  const installSkill = (item) => {
    const id = item.id || item.name || item.slug;
    return perform(
      id,
      () =>
        api("/api/skills/install", {
          method: "POST",
          body: JSON.stringify({
            id,
            force: false,
          }),
          timeoutMs: 300_000,
        }),
      "官方插件已安全安装",
    );
  };
  return (
    <section className={cx("skills-page", !embedded && "page", embedded && "embedded-skills-page")}>
      <div className={embedded ? "embedded-heading" : "page-heading"}>
        <div>
          <span className="eyebrow">{embedded ? "TOOLBOX EXTENSIONS" : "SKILLS HALL"}</span>
          <h1>{embedded ? "技能与插件" : "技能大厅"}</h1>
          <p>管理 Codex 已安装技能，并从 OpenAI 官方插件目录安全安装扩展。</p>
        </div>
        <button className="button secondary" onClick={() => load(true)} disabled={loading}>
          <RefreshCw className={loading ? "spin" : ""} size={16} />
          刷新目录
        </button>
      </div>
      <div className="view-tabs skill-tabs" role="tablist" aria-label="技能目录">
        <button className={tab === "installed" ? "active" : ""} onClick={() => setTab("installed")}>
          <BookOpen size={16} /> 已安装 <b>{installed.length}</b>
        </button>
        <button className={tab === "catalog" ? "active" : ""} onClick={() => setTab("catalog")}>
          <Boxes size={16} /> 官方目录 <b>{catalog.length}</b>
        </button>
      </div>
      <div className="skill-toolbar">
        <label className="search-input"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、说明、范围或路径" /></label>
        <span>显示 {filteredItems.length} / {activeItems.length}</span>
      </div>
      <ResourceState loading={loading && !installedPayload && !catalogPayload} error={error && !installedPayload && !catalogPayload ? error : ""} onRetry={() => load(true)} label="正在扫描技能与官方目录…" />
      {error && (installedPayload || catalogPayload) && (
        <div className="inline-notice warning"><AlertTriangle size={16} /><span>{error}；已展示可用的缓存结果。</span></div>
      )}
      {tab === "installed" ? (
        <div className="skill-grid">
          {visibleSkills.map((skill) => {
            const id = skill.id || skill.path || skill.name;
            const mutable = skill.mutable !== false && !["builtin", "plugin-cache", "system"].includes(skill.scope);
            const skillName = skillNameText(skill, id);
            const skillDescription = skillDescriptionText(skill);
            return (
              <article key={id} className={cx("skill-card", skill.enabled === false && "disabled") }>
                <header>
                  <span className="skill-icon"><Sparkles size={19} /></span>
                  <div><h2 title={skill.name || id}>{skillName}</h2><p>{skillDescription || "未提供技能说明"}</p></div>
                  <span className={cx("status-pill", skill.enabled === false ? "neutral" : "success")}>
                    {skill.enabled === false ? "已停用" : "已启用"}
                  </span>
                </header>
                <div className="skill-meta">
                  <span>{skillScopeLabel(skill.scopeLabelZh || skill.scopeZh || skill.scopeLabel || skill.scope)}</span>
                  <code title={skill.path}>{skill.path || "由 Codex 管理"}</code>
                </div>
                {(skill.error || skill.errors?.length) && <div className="skill-warning"><AlertTriangle size={15} />{skill.error || skill.errors.join("；")}</div>}
                <footer>
                  <button className="button secondary compact" disabled={workingId === id} onClick={() => toggleSkill(skill)}>
                    {workingId === id ? <Loader2 className="spin" size={14} /> : <Zap size={14} />}
                    {skill.enabled === false ? "启用" : "停用"}
                  </button>
                  {mutable && (
                    <button className="button danger-outline compact" disabled={workingId === id} onClick={() => deleteSkill(skill)}>
                      <Trash2 size={14} /> 删除
                    </button>
                  )}
                </footer>
              </article>
            );
          })}
          {!loading && !filteredItems.length && <div className="empty-state"><span><BookOpen size={28} /></span><h3>{query ? "没有匹配的技能" : "尚未发现可管理技能"}</h3><p>{query ? "尝试更短的关键词，或切换到官方目录。" : "安装后的用户技能会显示在这里；内置与插件缓存仅展示，不允许误删。"}</p></div>}
        </div>
      ) : (
        <div className="skill-grid catalog-grid">
          {visibleSkills.map((item) => {
            const id = item.id || item.name || item.slug;
            const itemName = skillNameText(item, id);
            const itemDescription = skillDescriptionText(item);
            const itemCategory = skillCategoryLabel(skillDisplayText(item, ["categoryZh", "category"], "扩展"));
            const isInstalled = Boolean(item.installed) || installedNames.has(String(itemName || id).toLowerCase()) || installedNames.has(String(item.name || id).toLowerCase());
            return (
              <article key={id} className="skill-card catalog-card">
                <header>
                  <span className="skill-icon official"><PlugZap size={19} /></span>
                  <div><h2 title={item.name || id}>{itemName}</h2><p>{itemDescription || (itemCategory + "类官方扩展")}</p></div>
                  <span className="status-pill success">官方源</span>
                </header>
                <div className="skill-meta"><span>{itemCategory}</span><code>{item.path || item.sourcePath || item.source?.path || "openai/plugins"}</code></div>
                <footer>
                  <button className="button primary compact" title={item.installHint || ""} disabled={isInstalled || item.installable === false || workingId === id} onClick={() => installSkill(item)}>
                    {workingId === id ? <Loader2 className="spin" size={14} /> : isInstalled ? <Check size={14} /> : <Download size={14} />}
                    {isInstalled ? "已安装" : item.installable === false ? "运行时暂不支持" : "安全安装"}
                  </button>
                </footer>
              </article>
            );
          })}
          {!loading && !filteredItems.length && <div className="empty-state"><span><Boxes size={28} /></span><h3>{query ? "没有匹配的官方插件" : "官方目录暂时不可用"}</h3><p>{query ? "清除搜索后查看完整目录。" : "可以稍后刷新；已安装技能不受影响。"}</p></div>}
        </div>
      )}
      <ToolPagination page={safeSkillPage} pageCount={skillPageCount} onChange={setPage} label="技能列表分页" />
    </section>
  );
}

function usageTokenCount(item) {
  const explicit = Number(item.totalTokens ?? item.total_tokens ?? item.tokens);
  if (Number.isFinite(explicit)) return explicit;
  // Cached input and reasoning output are subsets, not extra tokens.
  return (Number(item.inputTokens ?? item.input_tokens) || 0) + (Number(item.outputTokens ?? item.output_tokens) || 0);
}

function formatTokenCount(value) {
  const number = Math.max(0, Number(value) || 0);
  if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(number >= 10_000_000 ? 1 : 2)}M`;
  if (number >= 1_000) return `${(number / 1_000).toFixed(number >= 100_000 ? 0 : 1)}K`;
  return new Intl.NumberFormat("zh-CN").format(number);
}

let usageRangePreferenceCache = null;

function UsageView({ data, notify, confirm, embedded = false }) {
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [accountFilter, setAccountFilter] = useState("all");
  const [modelFilter, setModelFilter] = useState("all");
  const [usageRange, setUsageRange] = useState(() => normalizeUsageRange(usageRangePreferenceCache || data?.settings?.appBehavior?.usageRange));
  const rangeNow = new Date();
  const rangeWindow = resolveUsageRange(usageRange, rangeNow);
  const rangeSaveQueue = useRef(Promise.resolve());
  const commitUsageRange = preference => {
    usageRangePreferenceCache = preference;
    rangeSaveQueue.current = rangeSaveQueue.current.catch(() => {}).then(() => api("/api/app-behavior", {method:"POST",body:JSON.stringify({usageRange:preference})})).catch(cause => notify(`范围已用于当前视图，但保存失败：${cause.message}`, "warning"));
  };
  const [usageSource, setUsageSource] = useState("accounts");
  const [resetting, setResetting] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportResult, setExportResult] = useState(null);
  const [detailPage, setDetailPage] = useState(1);
  const usageLoadInFlight = useRef(false);
  const load = useCallback(async (force = false) => {
    if (usageLoadInFlight.current) return;
    if (!force && usageResourceCache) {
      setPayload(usageResourceCache);
      setLoading(false);
      return;
    }
    setLoading(true);
    usageLoadInFlight.current = true;
    setError("");
    try {
      const result = await api("/api/usage");
      usageResourceCache = result;
      setPayload(result);
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      usageLoadInFlight.current = false;
      setLoading(false);
    }
  }, []);
  useEffect(() => { load(); }, [load]);
  const gatewayPayload = payload?.usage && !Array.isArray(payload.usage) ? payload.usage : payload;
  const sessionPayload = gatewayPayload?.codexSessions;
  const accountPayload = gatewayPayload?.accountAttribution;
  const effectiveUsageSource = usageSource === "codex" && sessionPayload
    ? "codex"
    : accountPayload
      ? "accounts"
      : "gateway";
  const usagePayload = effectiveUsageSource === "codex"
    ? sessionPayload
    : effectiveUsageSource === "accounts"
      ? accountPayload
      : gatewayPayload;
  const usageReadWarnings = [gatewayPayload?.lastError, effectiveUsageSource !== "gateway" ? gatewayPayload?.codexSessionsError : null].filter(Boolean);
  const sessionCoverage = effectiveUsageSource !== "gateway" ? sessionPayload?.coverage : null;
  const historyPendingFiles = Number(sessionPayload?.coverage?.backfill?.pendingFiles) || 0;
  useEffect(() => {
    if (!historyPendingFiles) return undefined;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") load(true);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [historyPendingFiles, load]);
  useEffect(() => {
    setAccountFilter("all");
    setModelFilter("all");
    setDetailPage(1);
  }, [effectiveUsageSource]);
  const directRecords = firstArray(usagePayload, ["records", "items", "requests"]);
  const recentRecords = firstArray(usagePayload, ["recentRequests"]);
  const dayRecords = Object.entries(usagePayload?.days || {}).flatMap(([date, day]) =>
    Object.values(day?.routes || {}).map((route) => ({ ...route, date })),
  );
  const records = directRecords.length ? directRecords : dayRecords;
  const sourceLabels = usageSourceLabels(data?.settings?.accounts, data?.settings?.providers);
  const sourceName = (item) => {
    if (item.accountId) return sourceLabels.get(`account:${item.accountId}`) || `账号 ${item.accountId}`;
    if (item.providerId) return sourceLabels.get(`provider:${item.providerId}`) || `API ${item.providerId}`;
    if (effectiveUsageSource === "codex") return "Codex 日志未提供账号";
    return item.account || item.accountName || "未归因账号";
  };
  const normalizeRecord = (item, index, prefix = "row") => {
    const classification = item.requestClassification || item.classification || "unclassified";
    const role = item.agentRole ||
      (classification === "explicit_subagent" ? "subagent" :
        classification === "explicit_main_agent" ? "mainAgent" : "unclassified");
    const timestamp = String(item.timestamp || item.lastSeenAt || item.createdAt || item.date || item.day || "");
    const timestampMs = Date.parse(timestamp);
    return {
      ...item,
      rowId: item.id || `${prefix}-${timestamp || "row"}-${index}`,
      account: sourceName(item),
      model: item.model || item.modelName || item.routedModel || item.requestedModel || "未确定模型",
      timestamp,
      timestampMs: Number.isFinite(timestampMs) ? timestampMs : 0,
      date: usageRecordDateKey(item) || "未知日期",
      requests: Number(item.requests ?? item.requestCount ?? 1) || 0,
      tokens: usageTokenCount(item),
      classification,
      role,
      uncertain: Boolean(item.uncertain || role === "unclassified"),
    };
  };
  const normalized = records.map((item, index) => normalizeRecord(item, index, "summary"));
  const detailed = (recentRecords.length ? recentRecords : records)
    .map((item, index) => normalizeRecord(item, index, "detail"))
    .sort((left, right) => right.timestampMs - left.timestampMs);
  const accountDimension = effectiveUsageSource === "codex" ? "role" : "account";
  const dimensionLabel = (item) => accountDimension === "role"
    ? item.role === "mainAgent" ? "主代理" : item.role === "subagent" ? "子代理" : "未标记角色"
    : item.account;
  const dimensionKey = item => usageSourceKey(item, effectiveUsageSource);
  const accountLabels = new Map(normalized.map(item => [dimensionKey(item), dimensionLabel(item)]));
  const accounts = [...accountLabels.keys()].sort((left, right) => accountLabels.get(left).localeCompare(accountLabels.get(right)));
  const models = [...new Set(normalized.map((item) => item.model))].sort();
  const visible = filterUsageRecordsByRange(normalized.filter((item) =>
    (accountFilter === "all" || dimensionKey(item) === accountFilter) &&
    (modelFilter === "all" || item.model === modelFilter),
  ), usageRange, rangeNow);
  const visibleDetails = filterUsageRecordsByRange(detailed.filter((item) =>
    (accountFilter === "all" || dimensionKey(item) === accountFilter) &&
    (modelFilter === "all" || item.model === modelFilter),
  ), usageRange, rangeNow);
  useEffect(() => setDetailPage(1), [accountFilter, modelFilter, usageRange]);
  useEffect(() => setExportResult(null), [effectiveUsageSource, accountFilter, modelFilter, usageRange]);
  const detailPageSize = 20;
  const boundedDetails = visibleDetails.slice(0, detailPageSize * 5);
  const detailPageCount = Math.max(1, Math.ceil(boundedDetails.length / detailPageSize));
  const safeDetailPage = Math.min(detailPage, detailPageCount);
  const pagedDetails = boundedDetails.slice(
    (safeDetailPage - 1) * detailPageSize,
    safeDetailPage * detailPageSize,
  );
  const totals = visible.reduce((sum, item) => ({
    tokens: sum.tokens + item.tokens,
    requests: sum.requests + item.requests,
    uncertain: sum.uncertain + (item.uncertain ? item.tokens : 0),
    subagent: sum.subagent + (item.role === "subagent" ? item.tokens : 0),
    mainAgent: sum.mainAgent + (item.role === "mainAgent" ? item.tokens : 0),
    cached: sum.cached + Number(item.cachedInputTokens ?? item.cached_input_tokens ?? item.cachedTokens ?? item.cached_tokens ?? 0),
    cacheWrite: sum.cacheWrite + Number(item.cacheWriteTokens ?? item.cache_write_tokens ?? 0),
    reasoning: sum.reasoning + Number(item.reasoningOutputTokens ?? item.reasoning_output_tokens ?? item.reasoningTokens ?? 0),
    reported: sum.reported + Number(item.usageReportedCount ?? 0),
    missing: sum.missing + Number(item.usageMissingCount ?? 0),
  }), { tokens: 0, requests: 0, uncertain: 0, subagent: 0, mainAgent: 0, cached: 0, cacheWrite: 0, reasoning: 0, reported: 0, missing: 0 });
  const yearlyRecords = normalized.filter(item => (accountFilter === "all" || dimensionKey(item) === accountFilter) && (modelFilter === "all" || item.model === modelFilter));
  const dailyTotals = yearlyRecords.reduce((map, item) => {
    if (/^\d{4}-\d{2}-\d{2}$/.test(item.date))
      map.set(item.date, (map.get(item.date) || 0) + item.tokens);
    return map;
  }, new Map());
  const heatmapDays = useMemo(() => {
    const beijingParts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date()).reduce((parts, part) => ({ ...parts, [part.type]: part.value }), {});
    const today = new Date(Date.UTC(Number(beijingParts.year), Number(beijingParts.month) - 1, Number(beijingParts.day)));
    return Array.from({ length: 365 }, (_, index) => {
      const date = new Date(today);
      date.setUTCDate(today.getUTCDate() - (364 - index));
      const key = [date.getUTCFullYear(), String(date.getUTCMonth() + 1).padStart(2, "0"), String(date.getUTCDate()).padStart(2, "0")].join("-");
      return { key, tokens: dailyTotals.get(key) || 0 };
    });
  }, [yearlyRecords]);
  const heatmapMax = Math.max(0, ...heatmapDays.map((item) => item.tokens));
  const intensity = (tokens) => {
    if (!tokens || !heatmapMax) return 0;
    const ratio = tokens / heatmapMax;
    if (ratio >= 0.75) return 4;
    if (ratio >= 0.4) return 3;
    if (ratio >= 0.16) return 2;
    return 1;
  };
  const shareRows = (key, labelForKey = value => value) => {
    const buckets = visible.reduce((map, item) => {
      const label = typeof key === "function" ? key(item) : item[key];
      map.set(label, (map.get(label) || 0) + item.tokens);
      return map;
    }, new Map());
    return [...buckets.entries()]
      .map(([id, tokens]) => ({ id, label: labelForKey(id), tokens, ratio: totals.tokens ? tokens / totals.tokens : 0 }))
      .sort((left, right) => right.tokens - left.tokens)
      .slice(0, 6);
  };
  const accountShares = shareRows(dimensionKey, key => accountLabels.get(key) || key);
  const modelShares = shareRows("model");
  const exportUsage = async () => {
    setExporting(true);
    try {
      const response = await api("/api/usage/export-download", { method: "POST", body: JSON.stringify({ source: effectiveUsageSource, sourceFilter: accountFilter, model: modelFilter, date: "all", dateFrom:rangeWindow.startDate, dateTo:rangeWindow.endDate }) });
      if (!response.result?.path) throw new Error("未收到导出文件位置，请检查下载目录");
      setExportResult(response.result);
      notify("筛选汇总已导出到下载目录");
    } catch (cause) { notify(cause.message, "error"); }
    finally { setExporting(false); }
  };
  const resetUsage = async () => {
    const approved = await confirm({
      tone: "danger",
      title: "清空本地用量统计？",
      message: "只会清除 Agent Manager 本地网关记录，不会影响账号、额度或服务端账单。",
      detail: "清空前会自动创建加密恢复点，可在设置的“备份与恢复”中找回。",
      confirmLabel: "清空统计",
    });
    if (!approved) return;
    setResetting(true);
    try {
      await api("/api/usage/reset", { method: "POST", body: "{}" });
      usageResourceCache = null;
      notify("本地用量统计已清空，清空前的数据已保存为恢复点");
      await load(true);
    } catch (resetError) {
      notify(resetError.message, "error");
    } finally {
      setResetting(false);
    }
  };
  return (
    <section className={cx("usage-page", embedded ? "embedded-usage-page" : "page")}>
      <div className={embedded ? "usage-section-heading" : "page-heading"}>
        <div><span className="eyebrow">LOCAL USAGE</span><h2>{embedded ? "用量统计" : "用量统计"}</h2><p>Codex 会话总量、实际账号归因与主 / 子代理消耗。</p></div>
        <div className="heading-actions">
          <button className="button secondary" onClick={() => load(true)} disabled={loading}><RefreshCw className={loading ? "spin" : ""} size={16} />手动刷新</button>
          <button className="button secondary" onClick={exportUsage} disabled={exporting || loading || !visible.length}>{exporting ? <Loader2 size={16} className="spin" /> : <Download size={16} />}导出筛选汇总</button>
          <button className="button danger-outline" onClick={resetUsage} disabled={resetting}><Trash2 size={16} />清空统计</button>
        </div>
      </div>
      <div className="usage-source-tabs" role="tablist" aria-label="用量数据源">
        <button className={effectiveUsageSource === "codex" ? "active" : ""} disabled={!sessionPayload} onClick={() => setUsageSource("codex")}><strong>会话总量</strong><small>Codex 官方日志</small></button>
        <button className={effectiveUsageSource !== "codex" ? "active" : ""} onClick={() => setUsageSource("accounts")}><strong>账号归因</strong><small>实际消耗账号</small></button>
      </div>
      <UsageRangeControls value={usageRange} onChange={setUsageRange} onCommit={commitUsageRange} now={rangeNow} />
      <ResourceState loading={loading && !payload} error={error} onRetry={() => load(true)} label="正在汇总本地请求…" />
      {usageReadWarnings.map((warning, index) => <p key={index} className="usage-coverage-note" role="status">{warning}；当前显示仍可读取的统计。</p>)}
      {Number(sessionCoverage?.partialFiles) > 0 && <p className="usage-coverage-note" role="status">{historyPendingFiles > 0 ? `正在后台补全历史记录 · 剩余 ${historyPendingFiles} 个文件` : `正在读取新增记录 · 剩余 ${formatBytes(sessionCoverage.pendingBytes || 0)}`}</p>}
      {exportResult && <p className="usage-export-result">JSON 汇总已保存：<code>{exportResult.path}</code></p>}
      {payload && <>
        <div className="usage-kpis">
          <article><span><Gauge size={18} /></span><small>总 Token</small><strong>{formatTokenCount(totals.tokens)}</strong></article>
          <article><span><Bot size={18} /></span><small>主代理</small><strong>{formatTokenCount(totals.mainAgent)}</strong></article>
          <article><span><Route size={18} /></span><small>子代理</small><strong>{formatTokenCount(totals.subagent)}</strong></article>
          <article><span><Database size={18} /></span><small>缓存读取</small><strong>{formatTokenCount(totals.cached)}</strong></article>
          <article><span><BrainCircuit size={18} /></span><small>推理 Token</small><strong>{formatTokenCount(totals.reasoning)}</strong></article>
        </div>
        <div className="usage-coverage-strip">
          <span>请求 <b>{formatTokenCount(totals.requests)}</b></span>
          <span>用量回传 <b>{totals.requests ? `${((totals.reported / totals.requests) * 100).toFixed(1)}%` : "—"}</b></span>
          <span>角色可识别 <b>{totals.requests ? `${(((totals.requests - visible.filter((item) => item.role === "unclassified").reduce((sum, item) => sum + item.requests, 0)) / totals.requests) * 100).toFixed(1)}%` : "—"}</b></span>
          <span>缓存写入 <b>{usagePayload?.coverage?.cacheWriteAvailable === false ? "当前 Codex 日志未提供" : formatTokenCount(totals.cacheWrite)}</b></span>
        </div>
        <div className="usage-filters" aria-label="用量筛选">
          <label><span>{effectiveUsageSource === "codex" ? "代理角色" : "账号 / API 来源"}</span><select value={accountFilter} onChange={(event) => setAccountFilter(event.target.value)}><option value="all">全部{effectiveUsageSource === "codex" ? "角色" : "账号"}</option>{accounts.map((item) => <option key={item} value={item}>{accountLabels.get(item)}</option>)}</select></label>
          <label><span>模型</span><select value={modelFilter} onChange={(event) => setModelFilter(event.target.value)}><option value="all">全部模型</option>{models.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>

        </div>
        <section className="usage-heatmap-card">
          <header><div><small>365 DAY ACTIVITY</small><h3>请求日历热力格</h3></div><span>{heatmapDays.filter(item => dailyTotals.has(item.key)).length} 个有记录日期 · 最后更新 {formatDateTime(usagePayload?.updatedAt)}</span></header>
          <div className="usage-heatmap-scroll">
            <div className="usage-heatmap" aria-label={`最近 365 天${effectiveUsageSource === "codex" ? "会话" : "账号归因"} Token 热力图`}>
              {heatmapDays.map((item) => <span key={item.key} className={`level-${intensity(item.tokens)}`} title={`${item.key} · ${formatTokenCount(item.tokens)} Token`} />)}
            </div>
          </div>
          <footer><span>空白仅表示当前口径没有可统计记录</span><span className="heatmap-legend">少 <i className="level-0" /><i className="level-1" /><i className="level-2" /><i className="level-3" /><i className="level-4" /> 多</span></footer>
        </section>
        <div className="usage-share-grid">
          {[[effectiveUsageSource === "codex" ? "主 / 子代理占比" : "账号消耗占比", accountShares], ["模型占比", modelShares]].map(([title, rows]) => (
            <section className="usage-share-card" key={title}>
              <header><h3>{title}</h3><small>按筛选后 Token 排序，最多显示 6 项</small></header>
              <div>{rows.map((row) => <article key={row.id}><span><strong>{row.label}</strong><small>{formatTokenCount(row.tokens)} · {(row.ratio * 100).toFixed(row.ratio >= 0.1 ? 1 : 2)}%</small></span><i><b style={{ width: `${Math.max(row.ratio * 100, row.tokens ? 2 : 0)}%` }} /></i></article>)}</div>
              {!rows.length && <p>当前筛选条件没有可聚合记录。</p>}
            </section>
          ))}
        </div>
        <div className="usage-table-wrap">
          <table className="usage-table">
            <thead><tr><th>时间（最新在上）</th><th>{effectiveUsageSource === "codex" ? "日志来源" : "实际消耗账号"}</th><th>模型</th><th>角色</th><th>请求</th><th>输入</th><th>缓存读取</th><th>输出</th><th>推理</th><th>合计</th></tr></thead>
            <tbody>{pagedDetails.map((item) => <tr key={item.rowId} className={cx(item.uncertain && "uncertain", item.role === "mainAgent" && "main-agent-row", item.role === "subagent" && "subagent-row")}>
              <td>{item.timestamp.length > 10 ? formatDateTime(item.timestamp) : item.date}</td><td><strong>{item.account}</strong></td><td><code>{item.model}</code></td><td>{item.role === "mainAgent" ? <span className="agent-role-badge main"><Bot size={12} />主代理</span> : item.role === "subagent" ? <span className="agent-role-badge sub"><Route size={12} />子代理</span> : <span className="uncertain-tag">未标记</span>}</td><td>{formatTokenCount(item.requests)}</td><td>{formatTokenCount(item.inputTokens ?? item.input_tokens)}</td><td>{formatTokenCount(item.cachedInputTokens ?? item.cached_input_tokens ?? item.cachedTokens ?? item.cached_tokens)}</td><td>{formatTokenCount(item.outputTokens ?? item.output_tokens)}</td><td>{formatTokenCount(item.reasoningOutputTokens ?? item.reasoning_output_tokens ?? item.reasoningTokens)}</td><td><b>{formatTokenCount(item.tokens)}</b></td>
            </tr>)}</tbody>
          </table>
          {!visibleDetails.length && <div className="table-empty">当前筛选条件没有可显示记录。</div>}
        </div>
        {boundedDetails.length > 0 && <nav className="usage-pagination" aria-label="用量明细分页">
          <span>显示最新 {boundedDetails.length} 条 · 每页 20 条 · 最多 5 页</span>
          <div>
            <button className="icon-button" aria-label="上一页" disabled={safeDetailPage <= 1} onClick={() => setDetailPage((page) => Math.max(1, page - 1))}><ChevronLeft size={15} /></button>
            {Array.from({ length: detailPageCount }, (_, index) => index + 1).map((page) => <button key={page} className={cx("usage-page-number", page === safeDetailPage && "active")} aria-current={page === safeDetailPage ? "page" : undefined} onClick={() => setDetailPage(page)}>{page}</button>)}
            <button className="icon-button" aria-label="下一页" disabled={safeDetailPage >= detailPageCount} onClick={() => setDetailPage((page) => Math.min(detailPageCount, page + 1))}><ChevronRight size={15} /></button>
          </div>
        </nav>}
      </>}
    </section>
  );
}

function updateComponent(payload, key) {
  const components = payload?.status?.components || payload?.components || payload?.updates?.components || payload || {};
  return components[key] || components[key === "desktop" ? "codexDesktop" : "codexCli"] || {};
}

function hasAvailableUpdate(component) {
  if (typeof component?.updateAvailable === "boolean") return component.updateAvailable;
  if (["available", "outdated", "update_available"].includes(String(component?.status || "").toLowerCase())) return true;
  if (["current", "latest", "up_to_date", "up-to-date"].includes(String(component?.status || "").toLowerCase())) return false;
  if (component?.latestVersion && component?.currentVersion)
    return component.latestVersion !== component.currentVersion;
  if (component?.availableVersion && component?.installedVersion)
    return component.availableVersion !== component.installedVersion;
  return null;
}

function UpdateEmergencyPanel({ data, reload, notify, confirm }) {
  const loadGenerationRef = useRef(0);
  const [appRefreshKey, setAppRefreshKey] = useState(0);
  const [updates, setUpdates] = useState(null);
  const [checks, setChecks] = useState(null);
  const [validation, setValidation] = useState(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState("");
  const [error, setError] = useState("");
  const load = useCallback(async (force = false) => {
    const generation = ++loadGenerationRef.current;
    if (!force && maintenanceResourceCache) {
      setUpdates(maintenanceResourceCache.updates);
      setChecks(maintenanceResourceCache.checks);
      setValidation(maintenanceResourceCache.validation || null);
      setError(maintenanceResourceCache.error || "");
      setLoading(false);
      return;
    }
    setLoading(true);
    setError("");
    const updateRequest = force
      ? api("/api/updates/check", { method: "POST", body: "{}" })
      : api("/api/updates");
    const validationRequest = force
      ? api("/api/validate", {
          method: "POST",
          body: JSON.stringify({ runDoctor: false }),
        })
      : Promise.resolve(null);
    const [updateResult, checkResult, validationResult] = await Promise.allSettled([
      updateRequest,
      api(force ? "/api/emergency/checks?force=1" : "/api/emergency/checks"),
      validationRequest,
    ]);
    if (generation !== loadGenerationRef.current) return;
    const nextUpdates = updateResult.status === "fulfilled" ? updateResult.value : null;
    const nextChecks = checkResult.status === "fulfilled" ? checkResult.value : null;
    const nextValidation = validationResult.status === "fulfilled"
      ? validationResult.value?.validation || null
      : null;
    const nextError = [updateResult, checkResult, validationResult]
      .filter((item) => item.status === "rejected")
      .map((item) => item.reason?.message)
      .filter(Boolean)
      .join("；");
    if (nextUpdates) setUpdates(nextUpdates);
    if (nextChecks) setChecks(nextChecks);
    setValidation(nextValidation);
    setError(nextError);
    maintenanceResourceCache = {
      updates: nextUpdates,
      checks: nextChecks,
      validation: nextValidation,
      error: nextError,
    };
    setLoading(false);
  }, []);
  useEffect(() => {
    // Re-read local diagnostics on entry and after activation: the in-memory
    // startup promise may contain a pre-activation configuration warning.
    maintenanceResourceCache = null;
    load(false);
    return () => {
      ++loadGenerationRef.current;
    };
  }, [load, data.configurationSession?.status]);
  const desktop = updateComponent(updates, "desktop");
  const cli = updateComponent(updates, "cli");
  const desktopHasUpdate = hasAvailableUpdate(desktop);
  const cliHasUpdate = hasAvailableUpdate(cli);
  const cliStatus = String(cli.status || "").toLowerCase();
  const cliMissing =
    cli.available === false ||
    cli.installed === false ||
    ["missing", "not_found", "not-found", "absent"].includes(cliStatus) ||
    /未安装|未检测到|not found|missing/i.test(String(cli.message || ""));
  const configurationPending = ["waiting", "starting"].includes(data.configurationSession?.status);
  const diagnosticItems = firstArray(checks, ["checks", "items", "results"])
    .filter(item => !configurationPending || item.id !== "generated_configuration");
  const validationErrors = Array.isArray(validation?.errors) ? validation.errors : [];
  const validationWarnings = Array.isArray(validation?.warnings) ? validation.warnings : [];
  const validationRepairIds = validationErrors.some((message) =>
    /路由引用了不存在的 Agent/.test(String(message || "")),
  )
    ? ["settings_references"]
    : [];
  const validationItem = validation
    ? {
        id: "configuration_validation",
        label: validation.doctor && validation.doctor !== "未运行"
          ? "配置结构与 Codex Doctor"
          : "配置结构",
        status: validation.valid ? "ok" : "error",
        message: validation.valid
          ? validationWarnings[0] || validation.doctor || "配置结构检查通过。"
          : validationErrors[0] || "配置结构检查未通过。",
        autoFixable: validationRepairIds.length > 0,
        repairCheckIds: validationRepairIds,
      }
    : null;
  const checkItems = validationItem
    ? [...diagnosticItems, validationItem]
    : diagnosticItems;
  const checkIsHealthy = (item) => Boolean(item.ok ?? item.healthy ?? item.status === "ok");
  const issues = checkItems.filter((item) => !checkIsHealthy(item));
  const generatedConfigurationCheck = diagnosticItems.find(
    (item) => item.id === "generated_configuration",
  );
  // Fresh diagnostics are authoritative. The page-level status can be a
  // snapshot from before the user pressed “刷新状态” and must not count the
  // same configuration mismatch a second time.
  const configurationHealthy = generatedConfigurationCheck
    ? checkIsHealthy(generatedConfigurationCheck)
    : configurationPending || data.status?.fullyApplied !== false;
  const healthy = !configurationPending && Boolean(checkItems.length) && !issues.length && configurationHealthy;
  const checked = Boolean(checkItems.length) || !configurationHealthy;
  const modelCount = data.settings.modelWorkspace.mode === "aggregate"
    ? data.selectedModelKeys.length
    : data.modelSources
        .filter((source) => source.active)
        .flatMap((source) => source.models || [])
        .filter((model) => data.selectedModelKeys.includes(model.key)).length;
  const action = async (id, fn, successMessage, refreshAfter = true) => {
    setWorking(id);
    try {
      const result = await fn();
      notify(result?.message || result?.result?.message || successMessage);
      const embeddedStatus = result?.status || result?.result?.status;
      if (embeddedStatus?.components) {
        const nextUpdates = { ok: true, status: embeddedStatus };
        setUpdates(nextUpdates);
        maintenanceResourceCache = {
          ...(maintenanceResourceCache || {}),
          updates: nextUpdates,
        };
      }
      if (refreshAfter) {
        maintenanceResourceCache = null;
        await load(true);
      }
    } catch (actionError) {
      notify(actionError.message, "error");
    } finally {
      setWorking("");
    }
  };
  const runtimeRepairNeeded = issues.some((item) =>
    ["codex_runtime", "unsafe_cli_override"].includes(String(item.id || "")),
  );
  const fixable = issues
    .filter((item) => item.autoFixable)
    .flatMap((item) =>
      Array.isArray(item.repairCheckIds) && item.repairCheckIds.length
        ? item.repairCheckIds
        : [item.id],
    )
    .filter(Boolean)
    .filter((item, index, values) => values.indexOf(item) === index);
  const canRepair = runtimeRepairNeeded || fixable.length > 0;
  const repair = async () => {
    if (!canRepair) {
      notify("当前没有可自动修复的问题");
      return;
    }
    const approved = await confirm({
      title: "修复检测到的问题？",
      message: runtimeRepairNeeded
        ? "将修复 Codex 运行时，并处理其余可以安全自动修复的配置问题。"
        : `将处理 ${fixable.length} 个可以安全自动修复的配置问题。`,
      detail: "配置修改会创建备份并支持失败回滚；运行时只使用经过校验的官方组件，不会把包装脚本写入 CODEX_CLI_PATH。",
      confirmLabel: "修复问题",
    });
    if (!approved) return;
    await action(
      "repair",
      async () => {
        const messages = [];
        if (runtimeRepairNeeded) {
          const runtimeResult = await api("/api/codex-runtime/deploy", {
            method: "POST",
            body: "{}",
            timeoutMs: 600_000,
          });
          if (runtimeResult.message) messages.push(runtimeResult.message);
        }
        if (fixable.length) {
          const repairResult = await api("/api/emergency/repair", {
            method: "POST",
            body: JSON.stringify({ checkIds: fixable }),
            timeoutMs: 300_000,
          });
          if (repairResult.message || repairResult.result?.message)
            messages.push(repairResult.message || repairResult.result.message);
        }
        await reload();
        return { message: messages.join("；") || "检测到的问题已修复" };
      },
      "检测到的问题已修复",
    );
  };
  return (
    <section className="settings-card full maintenance-card">
      <header>
        <span><HardDriveDownload size={19} /></span>
        <div><h2>版本与维护</h2><p>查看和更新 Codex Desktop、CLI 与 Agent Manager</p></div>
        <button className="button secondary compact" onClick={() => { setAppRefreshKey(value => value + 1); load(true); }} disabled={loading || working}><RefreshCw className={loading ? "spin" : ""} size={14} />刷新状态</button>
      </header>
      {error && <div className="inline-notice warning"><AlertTriangle size={16} /><span>{error}</span></div>}
      {configurationPending && <div className="inline-notice"><Loader2 className="spin" size={16} /><span>正在激活临时配置，完成后自动核对同步状态。</span></div>}
      {checks?.stale && <div className="inline-notice"><Clock3 size={16} /><span>完整诊断缓存已过期；生成配置已重新核对，可点击“刷新状态”更新其他检查。</span></div>}
      <div className="update-grid">
        <AppUpdatePanel api={api} notify={notify} refreshKey={appRefreshKey} />
        <article className="update-component">
          <span className="update-icon codex"><img src="/codex-official.png" alt="" /></span>
          <div className="update-copy"><small>CODEX DESKTOP</small><strong>{desktop.currentVersion || desktop.installedVersion || "未检测"}</strong><p title={desktop.message}>{desktopHasUpdate === true ? `可更新到 ${desktop.latestVersion || desktop.availableVersion || "新版本"}` : desktopHasUpdate === false ? "Microsoft Store 确认已是最新版" : desktop.message || "刷新状态后检查更新"}</p></div>
          {desktopHasUpdate === true && desktop.canAutoUpdate ? <button className="button secondary compact" disabled={Boolean(working)} onClick={() => action("desktop", () => api("/api/updates/desktop", { method: "POST", body: "{}", timeoutMs: 960000 }), "Desktop 更新检查完成", false)}>{working === "desktop" ? <Loader2 className="spin" size={14} /> : <Download size={14} />}一键更新</button> : desktopHasUpdate === false ? <span className="status-pill success"><Check size={13} />已是最新版</span> : <button className="button secondary compact" disabled={loading || Boolean(working)} onClick={() => load(true)}><RefreshCw size={14} />检查更新</button>}

        </article>
        <article className="update-component">
          <span className="update-icon cli"><Bot size={20} /></span>
          <div className="update-copy"><small>CODEX CLI</small><strong>{cli.currentVersion || cli.installedVersion || (cliMissing ? "尚未安装" : "未检测")}</strong><p>{cliMissing ? "需要时可复用桌面运行时或部署官方 CLI" : cliHasUpdate === true ? `可更新到 ${cli.latestVersion || cli.availableVersion || cli.version || "最新版"}` : cliHasUpdate === false ? "独立 CLI" : cli.message || "刷新状态后确认版本"}</p></div>
          {cliMissing ? <button className="button primary compact" disabled={working === "cli-deploy"} onClick={async () => {
            const approved = await confirm({ title: "一键部署 Codex CLI？", message: "将先复用 Codex Desktop 内置运行时，再按环境选择官方安装方式并完成版本回验。", detail: "不会把 codex.cmd、codex.bat 或 codex.ps1 写入 CODEX_CLI_PATH；失败会保留现有环境并返回具体原因。", confirmLabel: "开始部署" });
            if (approved) await action("cli-deploy", () => api("/api/codex-runtime/deploy", {
              method: "POST",
              body: "{}",
              timeoutMs: 600_000,
            }), "Codex CLI 已部署并通过检查");
          }}>{working === "cli-deploy" ? <Loader2 className="spin" size={14} /> : <Download size={14} />}一键部署</button> : cliHasUpdate === true ? <button className="button secondary compact" disabled={working === "cli"} onClick={async () => {
            const approved = await confirm({ title: "更新 Codex CLI？", message: "将更新独立的 npm Codex CLI，不会重启 Codex Desktop。", detail: "不会创建或修改 CODEX_CLI_PATH；桌面端继续自动使用安装包内的原生 codex.exe。", confirmLabel: "更新 CLI" });
            if (approved) await action("cli", () => api("/api/updates/cli", {
              method: "POST",
              body: "{}",
              timeoutMs: 600_000,
            }), "Codex CLI 已更新");
          }}><Download size={14} />更新 CLI</button> : cliHasUpdate === false ? <span className="status-pill success"><Check size={13} />已是最新版</span> : <span className="status-pill">待刷新</span>}
        </article>
      </div>
      <div className={cx("repair-section", healthy && "healthy")}>
        <div className="repair-heading"><span>{healthy ? <ShieldCheck size={18} /> : <ShieldAlert size={18} />}</span><div><strong>{configurationPending ? "正在激活临时配置" : !checked ? "尚未检查配置" : healthy ? "Codex 配置健康" : "Codex 配置需要处理"}</strong><small>{configurationPending ? "激活完成后自动核对；其他诊断结果仍列于下方" : !checked ? "刷新状态后查看具体检查结果" : healthy ? `配置已同步 · ${modelCount} 个当前可用模型 · ${data.codexVersion || "版本已检测"}` : issues.length ? `发现 ${issues.length} 个配置或运行问题 · ${canRepair ? "可以自动修复" : "请按下方提示处理"}` : "配置尚未同步，请重新检查后应用"}</small></div>{!healthy && canRepair ? <button className="button primary" disabled={working === "repair"} onClick={repair}>{working === "repair" ? <Loader2 className="spin" size={15} /> : <Wrench size={15} />}修复问题</button> : !healthy ? <span className={cx("status-pill", checked && "warning")}>{checked ? "需按提示处理" : "待检查"}</span> : null}</div>
        {!healthy && <div className="repair-checks">
          {checkItems.map((item, index) => {
            const ok = item.ok ?? item.healthy ?? item.status === "ok";
            return <article key={item.id || item.name || index} className={cx(ok ? "ok" : "issue", item.severity && `severity-${item.severity}`)}><span>{ok ? <Check size={15} /> : <AlertTriangle size={15} />}</span><div><strong>{item.label || item.title || item.name || item.id || "检查项"}</strong><small>{item.message || item.detail || (ok ? "正常" : "需要处理")}</small></div></article>;
          })}
          {!loading && !checkItems.length && <p className="repair-empty">尚无诊断结果，点击“刷新状态”重新扫描。</p>}
        </div>}
      </div>
    </section>
  );
}

function SettingsView({ data, reload, notify, confirm, setBusy, busy }) {
  const [closeToTray, setCloseToTray] = useState(
    Boolean(data.settings.appBehavior?.closeToTray),
  );
  const [quotaRefreshMinutes, setQuotaRefreshMinutes] = useState(
    Number(data.settings.appBehavior?.quotaRefreshMinutes ?? 10),
  );
  const [mailHealthCheckHours, setMailHealthCheckHours] = useState(
    Number(data.settings.appBehavior?.mailHealthCheckHours ?? 24),
  );
  useEffect(() => {
    setCloseToTray(Boolean(data.settings.appBehavior?.closeToTray));
    setQuotaRefreshMinutes(
      Number(data.settings.appBehavior?.quotaRefreshMinutes ?? 10),
    );
    setMailHealthCheckHours(
      Number(data.settings.appBehavior?.mailHealthCheckHours ?? 24),
    );
  }, [
    data.settings.appBehavior?.closeToTray,
    data.settings.appBehavior?.quotaRefreshMinutes,
    data.settings.appBehavior?.mailHealthCheckHours,
  ]);
  const saveBehavior = async (checked) => {
    setCloseToTray(checked);
    try {
      await api("/api/app-behavior", {
        method: "POST",
        body: JSON.stringify({ closeToTray: checked }),
      });
      notify(checked ? "关闭按钮将最小化到系统托盘" : "关闭按钮将关闭 Codex 并恢复原始配置");
      await reload();
    } catch (error) {
      setCloseToTray(!checked);
      notify(error.message, "error");
    }
  };
  const saveQuotaRefresh = async (event) => {
    const nextValue = Number(event.target.value);
    const previousValue = quotaRefreshMinutes;
    setQuotaRefreshMinutes(nextValue);
    try {
      await api("/api/app-behavior", {
        method: "POST",
        body: JSON.stringify({ quotaRefreshMinutes: nextValue }),
      });
      notify(
        nextValue === 0
          ? "账号额度自动刷新已关闭"
          : `账号额度将每 ${nextValue} 分钟自动刷新`,
      );
      await reload();
    } catch (error) {
      setQuotaRefreshMinutes(previousValue);
      notify(error.message, "error");
    }
  };
  const saveMailHealthCheck = async (event) => {
    const nextValue = Number(event.target.value);
    const previousValue = mailHealthCheckHours;
    setMailHealthCheckHours(nextValue);
    try {
      await api("/api/app-behavior", {
        method: "POST",
        body: JSON.stringify({ mailHealthCheckHours: nextValue }),
      });
      notify(
        nextValue === 0
          ? "邮箱自动登录检查已关闭"
          : `邮箱登录状态将每 ${nextValue} 小时检查一次`,
      );
      await reload();
    } catch (error) {
      setMailHealthCheckHours(previousValue);
      notify(error.message, "error");
    }
  };
  const quickRestart = async () => {
    const approved = await confirm({
      tone: "warning",
      title: "快速重启 Agent Manager？",
      message: "这只会重启管理器及其本地后台服务，不会关闭或重新打开 Codex。",
      detail: "当前管理器页面会短暂断开，服务恢复后可重新访问。",
      confirmLabel: "快速重启",
    });
    if (!approved) return;
    setBusy(true);
    try {
      notify("正在预热新窗口，当前服务会尽量保持可用");
      const restart = await api("/api/quick-restart", { method: "POST", body: "{}", timeoutMs: 60_000 });
      notify("管理器正在快速重启；Codex 保持当前状态");
      const requestedAt = Date.parse(restart.requestedAt || "") || Date.now();
      const deadline = Date.now() + 35_000;
      let terminalError = "";
      while (Date.now() < deadline) {
        await new Promise((resolve) => window.setTimeout(resolve, 500));
        try {
          const lifecycle = await api("/api/app-lifecycle", { timeoutMs: 1_500 });
          const shutdown = lifecycle.shutdown || {};
          const statusAt = Date.parse(shutdown.at || "") || 0;
          if (statusAt + 2_000 < requestedAt) continue;
          if (shutdown.phase === "quick-restart-rolled-back") {
            terminalError = `快速重启失败，旧管理器已安全恢复${
              shutdown.errors?.length ? `：${shutdown.errors.join("；")}` : "。"
            }`;
            break;
          }
          if (shutdown.phase === "blocked-codex-still-running") {
            terminalError = `快速重启已取消${
              shutdown.errors?.length ? `：${shutdown.errors.join("；")}` : "；Codex 保持运行。"
            }`;
            break;
          }
        } catch {
          // A temporary connection failure is expected while the old local
          // server releases its port and the verified replacement starts.
        }
      }
      if (terminalError) throw new Error(terminalError);
      // On success the old WebView is destroyed and this code never reaches
      // the deadline. Reaching it means the same window survived the entire
      // handoff, so do not leave a misleading permanent success toast.
      throw new Error("快速重启在 35 秒内没有完成；旧管理器和 Codex 均保持运行，请刷新状态后重试。");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="page settings-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">SETTINGS</span>
          <h1>设置</h1>
            <p>管理本地运行、备份恢复与 Codex 配置健康。</p>
        </div>
      </div>
      <div className="settings-grid">
        <AppearanceControls onSave={async appearance => {
          try { await api("/api/app-behavior", { method: "POST", body: JSON.stringify({ appearance }) }); }
          catch (error) { notify(error.message, "error"); throw error; }
          await reload().catch(error => notify(`外观已保存，状态读取失败：${error.message}`, "warning"));
        }} />
        <section className="settings-card full settings-behavior-card">
          <header>
            <span>
              <Settings size={19} />
            </span>
            <div>
              <h2>应用行为</h2>
              <p>窗口关闭与后台运行</p>
            </div>
          </header>
          <div className="setting-row static">
            <div>
              <strong>子代理临时配置</strong>
              <small>
                {data.configurationSession?.error ||
                  data.configurationSession?.message ||
                  "窗口就绪后自动应用。正在运行的 Codex 任务保持不变，新配置在下次启动时生效。"}
              </small>
            </div>
            <span
              className={cx(
                "status-pill",
                data.configurationSession?.active ? "success" : "warning",
              )}
            >
              {data.configurationSession?.active ? (
                <>
                  <Check size={13} />
                  {data.configurationSession?.externalSelectionPreserved ? "沿用当前配置" : "已临时应用"}
                </>
              ) : (
                "未应用"
              )}
            </span>
            {data.configurationSession?.status === "error" && <button className="button secondary compact" disabled={busy} onClick={async () => {
              setBusy(true);
              try {
                const result = await api("/api/configuration-session/retry", { method: "POST", body: "{}" });
                await reload();
                if (!result.configurationSession?.active) throw new Error(result.configurationSession?.error || "配置仍未应用，请稍后重试");
                notify("临时配置已恢复，现有 Codex 任务保持运行");
              } catch (error) { notify(error.message, "error"); } finally { setBusy(false); }
            }}>重试应用</button>}
          </div>
          <label className="setting-row">
            <div>
              <strong>关闭后最小化到系统托盘</strong>
              <small>开启时关闭窗口会留在托盘并保持 Codex 运行；关闭此选项时，关闭窗口将关闭 Codex 并恢复原始配置。</small>
            </div>
            <Switch checked={closeToTray} onChange={saveBehavior} />
          </label>
          {closeToTray && (
            <div className="setting-row static">
              <div>
                <strong>托盘运行状态</strong>
                <small>
                  {data.trayStatus?.error ||
                    (data.trayStatus?.ready
                      ? "关闭主窗口后可从托盘恢复。"
                      : "正在初始化托盘图标。")}
                </small>
              </div>
              <span
                className={cx(
                  "status-pill",
                  data.trayStatus?.ready
                    ? "success"
                    : data.trayStatus?.error
                      ? "warning"
                      : "neutral",
                )}
              >
                {data.trayStatus?.ready ? (
                  <>
                    <Check size={13} />
                    已就绪
                  </>
                ) : data.trayStatus?.error ? (
                  "异常"
                ) : (
                  "初始化"
                )}
              </span>
            </div>
          )}
          <label className="setting-row">
            <div>
              <strong>账号额度自动刷新</strong>
              <small>
                后台常规只刷新周额度；模型、订阅和能力探测使用长缓存。默认 10 分钟，HTTP 429 至少退避 30 分钟。
                {data.accountAutoRefreshStatus?.lastCheckedAt
                  ? ` 上次检查：${formatDateTime(data.accountAutoRefreshStatus.lastCheckedAt)}`
                  : ""}
              </small>
            </div>
            <select
              className="setting-select"
              aria-label="账号额度自动刷新间隔"
              value={quotaRefreshMinutes}
              onChange={saveQuotaRefresh}
            >
              <option value={0}>关闭</option>
              <option value={5}>5 分钟（较频繁）</option>
              <option value={10}>10 分钟（推荐）</option>
              <option value={30}>30 分钟</option>
              <option value={60}>60 分钟（最省请求）</option>
            </select>
          </label>
          <label className="setting-row">
            <div>
              <strong>邮箱登录自动健康检查</strong>
              <small>
                只验证安全连接、登录和只读打开收件箱，不搜索或下载邮件；逐个串行检查，默认每 24 小时一次。
                {data.mailHealthStatus?.lastCheckedAt
                  ? ` 上次调度：${formatDateTime(data.mailHealthStatus.lastCheckedAt)}`
                  : ""}
              </small>
            </div>
            <select
              className="setting-select"
              aria-label="邮箱登录自动健康检查间隔"
              value={mailHealthCheckHours}
              onChange={saveMailHealthCheck}
            >
              <option value={0}>关闭</option>
              <option value={12}>12 小时</option>
              <option value={24}>24 小时（推荐）</option>
              <option value={48}>48 小时</option>
              <option value={168}>每周</option>
            </select>
          </label>
          <div className="setting-row static">
            <div>
              <strong>静默运行外部命令</strong>
              <small>OAuth、Codex 探测和应用启动均使用无控制台窗口模式。</small>
            </div>
            <span className="status-pill success">
              <Check size={13} />
              已启用
            </span>
          </div>
        </section>
        <UpdateEmergencyPanel data={data} reload={reload} notify={notify} confirm={confirm} />
        <RecoveryPanel api={api} notify={notify} confirm={confirm} onRestored={reload} gatewayRunning={data.web2apiStatus.running} />
        <section className="settings-card danger-zone full">
          <header>
            <span>
              <LogOut size={19} />
            </span>
            <div>
              <h2>退出应用</h2>
              <p>可仅重启管理器，或停止托盘、本地 API 与后台服务</p>
            </div>
          </header>
          <div className="danger-zone-actions">
            <button
              className="button secondary"
              disabled={busy}
              onClick={quickRestart}
            >
              <RefreshCw size={16} />
              快速重启
            </button>
            <button
              className="button secondary"
              disabled={busy}
              onClick={async () => {
                const approved = await confirm({
                  tone: "warning",
                  title: "仅退出 Agent Manager？",
                  message: "只退出管理器，不关闭当前 Codex，也不还原当前临时配置。",
                  detail: data.web2apiStatus.running
                    ? "本地 API 会随管理器停止；若 Codex 正在使用聚合、子代理或中转站路由，请在继续对话前重新打开 Agent Manager。"
                    : "下次打开管理器会继续接管并校验当前配置。",
                  confirmLabel: "仅退出软件",
                });
                if (approved)
                  await api("/api/exit-only", { method: "POST", body: "{}" });
              }}
            >
              <X size={16} />
              仅退出软件
            </button>
            <button
              className="button danger-outline"
              disabled={busy}
              onClick={async () => {
                const approved = await confirm({
                  tone: "danger",
                  title: "彻底退出 Agent Manager？",
                  message: "应用窗口、系统托盘、本地 API 服务和正在运行的 Codex 都会停止。",
                  detail: "默认 Codex 配置会被还原，但不会自动重新打开 Codex；账号与管理器配置会保留。",
                  confirmLabel: "彻底退出",
                });
                if (approved)
                  await api("/api/shutdown", { method: "POST", body: "{}" });
              }}
            >
              <LogOut size={16} />
              彻底退出
            </button>
          </div>
        </section>
      </div>
    </section>
  );
}

function ClaudeBrandMark() {
  return (
    <svg viewBox="0 0 64 64" aria-hidden="true">
      <g stroke="currentColor" strokeWidth="6" strokeLinecap="round">
        <path d="M32 8v48M8 32h48M15 15l34 34M49 15 15 49" />
        <path d="m22 7 20 50M7 22l50 20M42 7 22 57M7 42l50-20" strokeWidth="3.6" />
      </g>
    </svg>
  );
}

function ClaudeWorkspace({ onBack, notify, confirm }) {
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [working, setWorking] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [draft, setDraft] = useState({ name: "", baseUrl: "", apiKey: "", models: "" });
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try { setPayload(await api("/api/claude/profiles")); }
    catch (loadError) { setError(loadError.message); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);
  const profiles = firstArray(payload, ["profiles", "items"]);
  const providedStatus = payload?.status || payload?.claudeStatus || {};
  const activeId = payload?.activeProfileId || providedStatus.activeProfileId || "official";
  const status = Object.keys(providedStatus).length ? providedStatus : {
    running: Boolean(payload?.desktopRunning),
    modeLabel: activeId && activeId !== "official" ? "第三方直连" : "官方 Claude Desktop",
    message: payload?.desktopRunning ? "Claude Desktop 正在运行；切换前请先完全退出。" : "Claude Desktop 已停止，可以安全切换配置。",
  };
  const perform = async (id, fn, success) => {
    setWorking(id);
    try {
      const result = await fn();
      notify(result?.message || success);
      await load();
      return true;
    } catch (actionError) {
      notify(actionError.message, "error");
      return false;
    } finally { setWorking(""); }
  };
  const createProfile = async (event) => {
    event.preventDefault();
    if (!draft.name.trim() || !draft.baseUrl.trim() || !draft.apiKey.trim()) {
      notify("请填写名称、API 地址和 API Key", "error");
      return;
    }
    const saved = await perform("create", () => api("/api/claude/profiles", {
      method: "POST",
      body: JSON.stringify({
        ...draft,
        models: draft.models.split(/[,\n]/).map((item) => item.trim()).filter(Boolean),
      }),
    }), "Claude 3P 直连配置已保存");
    if (saved) {
      setDraft({ name: "", baseUrl: "", apiKey: "", models: "" });
      setShowCreate(false);
    }
  };
  const applyProfile = async (profile) => {
    const approved = await confirm({
      title: `应用“${profile.name || profile.label}”？`,
      message: "会在 Claude Desktop 完全停止后，以事务方式写入第三方直连配置。",
      detail: "应用前会备份现有配置；失败会自动回滚。不会捕获或替换 Anthropic 官方 OAuth 登录。",
      confirmLabel: "应用配置",
    });
    if (approved) await perform(profile.id, () => api("/api/claude/apply", { method: "POST", body: JSON.stringify({ id: profile.id }) }), "Claude Desktop 直连配置已应用");
  };
  const restoreOfficial = async () => {
    const approved = await confirm({
      title: "恢复 Claude 官方模式？",
      message: "只移除 Agent Manager 管理的第三方配置，恢复 Claude Desktop 官方启动方式。",
      detail: "官方 OAuth 账号仍需在 Claude Desktop 内登录与切换，本应用不会读取登录 Cookie 或 Token。",
      confirmLabel: "恢复官方模式",
    });
    if (approved) await perform("restore", () => api("/api/claude/restore", { method: "POST", body: "{}" }), "已恢复 Claude Desktop 官方模式");
  };
  const removeProfile = async (profile) => {
    const approved = await confirm({ tone: "danger", title: `删除“${profile.name || profile.label}”？`, message: "只删除 Agent Manager 保存的直连配置，不影响 Claude 官方账号。", confirmLabel: "删除配置" });
    if (approved) await perform(profile.id, () => api(`/api/claude/profiles/${encodeURIComponent(profile.id)}`, { method: "DELETE", body: "{}" }), "配置已删除");
  };
  const previewImport = async () => {
    setWorking("preview");
    try {
      const result = await api("/api/claude/import-preview");
      const preview = result.preview || {};
      if (!preview.available) {
        notify(preview.warning || "没有发现可导入的 Claude Code / CCSwitch 第三方配置", "error");
        return;
      }
      setDraft({
        name: preview.name || "从 Claude Code 导入",
        baseUrl: preview.baseUrl || "",
        apiKey: "",
        models: (preview.models || []).join(", "),
      });
      setShowCreate(true);
      notify("已读取第三方地址与模型；出于安全原因请重新输入 API Key");
    } catch (previewError) {
      notify(previewError.message, "error");
    } finally {
      setWorking("");
    }
  };
  return (
    <div className="claude-workspace">
      <aside className="claude-sidebar">
        <button className="brand" onClick={onBack} title="返回 Agent Manager 首页"><img src="/app-icon.png" alt="" /><div><strong>Agent</strong><span>Manager</span></div></button>
        <div className="claude-platform"><span><ClaudeBrandMark /></span><div><small>ANTHROPIC</small><strong>Claude Desktop</strong></div></div>
        <nav><button className="active"><span><Settings size={19} /></span><div><strong>Desktop 工作台</strong><small>官方恢复与 3P 直连</small></div><i /></button></nav>
        <div className="claude-safety"><ShieldCheck size={18} /><span><strong>OAuth 边界</strong><small>官方账号只在 Claude Desktop 内登录与切换</small></span></div>
      </aside>
      <main id="main-content" className="claude-main">
        <section className="page claude-page">
          <div className="page-heading"><div><span className="eyebrow claude">CLAUDE DESKTOP</span><h1>Claude 工作台</h1><p>安全管理官方模式恢复和第三方 API 直连配置；不读取、不转换官方 OAuth 凭据。</p></div><div className="heading-actions"><button className="button secondary" onClick={previewImport} disabled={working === "preview"}><Download size={16} />扫描 Claude Code / CCSwitch</button><button className="button secondary" onClick={restoreOfficial} disabled={working === "restore"}><RotateCcw size={16} />恢复官方模式</button><button className="button claude-primary" onClick={() => setShowCreate(true)}><Plus size={16} />新建 3P 配置</button></div></div>
          <div className="claude-boundary"><ShieldAlert size={20} /><div><strong>官方 OAuth 不可由 Agent Manager 切换</strong><p>Claude Desktop 的官方登录状态由 Anthropic 客户端管理。本工作台只提供“恢复官方配置”和经过备份、校验、回滚的第三方直连配置。</p></div></div>
          <ResourceState loading={loading && !payload} error={error} onRetry={load} label="正在读取 Claude Desktop 配置…" />
          {payload && <>
            <section className="claude-runtime-card"><span className="claude-runtime-icon"><ClaudeBrandMark /></span><div><small>当前模式</small><strong>{status.modeLabel || (activeId ? "第三方直连" : "官方 Claude Desktop")}</strong><p>{status.message || status.executable || "配置写入前会等待 Claude Desktop 完全退出"}</p></div><span className={cx("status-pill", status.error ? "warning" : "success")}>{status.error ? "需要检查" : status.running ? "正在运行" : "可安全配置"}</span></section>
            <div className="claude-profile-grid">
              {profiles.map((profile) => <article key={profile.id} className={cx("claude-profile-card", activeId === profile.id && "active", profile.mode === "official" && "official")}>
                <header><span>{profile.mode === "official" ? <ClaudeBrandMark /> : <Server size={18} />}</span><div><h2>{profile.name || profile.label || "未命名配置"}</h2><p>{profile.mode === "official" ? profile.description : (profile.models || []).map((item) => typeof item === "string" ? item : item.name).filter(Boolean).join(" · ") || "跟随服务端模型"}</p></div>{activeId === profile.id && <span className="status-pill success"><Check size={13} />当前</span>}</header>
                <dl>{profile.mode === "official" ? <><div><dt>登录方式</dt><dd>在 Claude Desktop 内完成官方 OAuth</dd></div><div><dt>本应用权限</dt><dd>仅恢复官方配置，不读取会话</dd></div></> : <><div><dt>API 地址</dt><dd>{profile.baseUrl || "未提供"}</dd></div><div><dt>凭据</dt><dd>{profile.apiKeyConfigured === false ? "未配置" : "已加密保存"}</dd></div></>}</dl>
                <footer><button className={cx("button compact", profile.mode === "official" ? "secondary" : "claude-primary")} disabled={working === profile.id || activeId === profile.id} onClick={() => profile.mode === "official" ? restoreOfficial() : applyProfile(profile)}>{working === profile.id ? <Loader2 className="spin" size={14} /> : profile.mode === "official" ? <RotateCcw size={14} /> : <Play size={14} />}{activeId === profile.id ? "正在使用" : profile.mode === "official" ? "恢复官方模式" : "应用到 Desktop"}</button>{profile.mode !== "official" && <button className="button danger-outline compact" disabled={working === profile.id} onClick={() => removeProfile(profile)}><Trash2 size={14} />删除</button>}</footer>
              </article>)}
              {!profiles.length && <div className="empty-state"><span className="claude-empty"><ClaudeBrandMark /></span><h3>还没有第三方直连配置</h3><p>官方 Claude Desktop 可直接使用；需要中转站或兼容 API 时再新建配置。</p></div>}
            </div>
          </>}
        </section>
      </main>
      {showCreate && <Modal title="新建 Claude 3P 直连" description="仅保存兼容 Anthropic API 的推理服务；不会导入官方 OAuth。" onClose={() => setShowCreate(false)}>
        <form className="modal-body claude-profile-form" onSubmit={createProfile}>
          <label><span>配置名称</span><input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="例如：团队中转站" autoFocus /></label>
          <label><span>API 地址</span><input value={draft.baseUrl} onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })} placeholder="https://api.example.com" /></label>
          <label><span>API Key</span><input type="password" value={draft.apiKey} onChange={(event) => setDraft({ ...draft, apiKey: event.target.value })} placeholder="输入后由本机安全保存" autoComplete="off" /></label>
          <label><span>模型列表（可选，逗号分隔）</span><input value={draft.models} onChange={(event) => setDraft({ ...draft, models: event.target.value })} placeholder="claude-sonnet-4-5, claude-opus-4-1" /></label>
          <div className="form-notice"><ShieldCheck size={16} /><span>应用时会校验配置、原子写入并读取回验；任何一步失败都会恢复备份。</span></div>
          <div className="modal-actions"><button type="button" className="button secondary" onClick={() => setShowCreate(false)}>取消</button><button className="button claude-primary" disabled={working === "create"}>{working === "create" ? <Loader2 className="spin" size={15} /> : <Save size={15} />}保存配置</button></div>
        </form>
      </Modal>}
    </div>
  );
}

function ProductHome({ onOpenCodex, onOpenClaude }) {
  return (
    <main id="main-content" className="product-home">
      <header className="home-brand">
        <img src="/app-icon.png" alt="" />
        <span>
          <strong>Agent Manager</strong>
          <small>一个入口，管理你的 AI 开发工作台</small>
        </span>
      </header>
      <section className="home-hero">
        <span className="eyebrow">CHOOSE YOUR WORKSPACE</span>
        <h1>今天使用哪个 Agent？</h1>
        <p>账号、模型、调度、会话与工具统一管理。选择平台后进入对应工作区。</p>
      </section>
      <section className="product-grid" aria-label="Agent 平台">
        <button className="product-card codex-product" onClick={onOpenCodex}>
          <span className="product-logo"><img src="/codex-official.png" alt="Codex" /></span>
          <span className="product-copy">
            <small>OPENAI</small>
            <strong>Codex</strong>
            <em>账号池 · 模型聚合 · 子代理调度</em>
          </span>
          <span className="product-enter">
            进入工作区 <ChevronRight size={19} />
          </span>
        </button>
        <button className="product-card claude-product" onClick={onOpenClaude}>
          <span className="product-logo"><img src="/claude-official.svg" alt="Claude" /></span>
          <span className="product-copy">
            <small>ANTHROPIC</small>
            <strong>Claude</strong>
            <em>Claude 账号与调度工作区</em>
          </span>
          <span className="product-enter">进入工作区 <ChevronRight size={19} /></span>
        </button>
      </section>
      <footer className="home-footer">Agent Manager {APP_VERSION} · 本地优先 · 凭据加密保存</footer>
    </main>
  );
}

export default function App() {
  const [data, setData] = useState(null);
  useSavedAppearance(data?.settings?.appBehavior?.appearance);
  const [view, setView] = useState("accounts");
  const [workspace, setWorkspace] = useState("home");
  useLayoutEffect(() => { document.documentElement.dataset.workspace = workspace; }, [workspace]);
  useNativeWindowAppearance(api, Boolean(data));
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState(null);
  const [confirmDialog, setConfirmDialog] = useState(null);
  const confirmationQueueRef = useRef(null);
  if (!confirmationQueueRef.current) confirmationQueueRef.current = createConfirmationQueue(setConfirmDialog);
  const [sessionHistory, setSessionHistory] = useState(null);
  const orchestrationDraftRef = useRef(null);
  const reloadGenerationRef = useRef(0);
  const snapshotGenerationRef = useRef(0);
  const notify = useCallback((message, kind = "success") => {
    setToast({ message, kind, id: Date.now() });
  }, []);
  useEffect(() => {
    if (!toast) return undefined;
    const timer = setTimeout(() => setToast(null), 4200);
    return () => clearTimeout(timer);
  }, [toast]);
  const reload = useCallback(async (options = {}) => {
    const generation = ++reloadGenerationRef.current;
    ++snapshotGenerationRef.current;
    const result = await api("/api/state");
    if (generation !== reloadGenerationRef.current) return result;
    ++snapshotGenerationRef.current;
    setData((current) => {
      if (!options.preserveWorkspace || !current) return result;
      return {
        ...result,
        settings: {
          ...result.settings,
          modelWorkspace: current.settings.modelWorkspace,
          strategies: current.settings.strategies,
        },
        modelSources: current.modelSources,
        selectedModelKeys: current.selectedModelKeys,
        effectiveSubagentRouting: current.effectiveSubagentRouting,
        localModels: current.localModels,
        efforts: current.efforts,
      };
    });
    return result;
  }, []);
  const reloadConnections = useCallback(async () => {
    const generation = ++reloadGenerationRef.current;
    ++snapshotGenerationRef.current;
    const snapshot = await api("/api/connections");
    if (generation !== reloadGenerationRef.current) return snapshot;
    ++snapshotGenerationRef.current;
    setData(current => current ? {
      ...current,
      settings: { ...current.settings, ...snapshot.settings },
      modelSources: snapshot.modelSources || current.modelSources,
      selectedModelKeys: snapshot.selectedModelKeys || current.selectedModelKeys,
      effectiveSubagentRouting: snapshot.effectiveSubagentRouting || current.effectiveSubagentRouting,
      auth: snapshot.auth || current.auth,
    } : current);
    return snapshot;
  }, []);
  const updateData = useCallback(
    (updater, options) => {
      if (options?.invalidateSnapshots) {
        ++reloadGenerationRef.current;
        ++snapshotGenerationRef.current;
      }
      setData((current) =>
        typeof updater === "function" ? updater(current) : updater,
      );
    },
    [],
  );
  const refreshAccountSnapshot = useCallback(async () => {
    const generation = ++snapshotGenerationRef.current;
    const result = await api("/api/accounts/snapshot");
    if (generation !== snapshotGenerationRef.current) return result;
    updateData((current) => {
      if (!current) return current;
      return {
        ...current,
        settings: {
          ...current.settings,
          accounts: Array.isArray(result.accounts)
            ? result.accounts
            : current.settings.accounts,
        },
        auth: result.auth || current.auth,
        accountRefreshStatus:
          result.accountRefreshStatus || current.accountRefreshStatus,
        accountAutoRefreshStatus:
          result.accountAutoRefreshStatus || current.accountAutoRefreshStatus,
        accountSnapshotRevision:
          result.revision ?? current.accountSnapshotRevision,
      };
    });
    return result;
  }, [updateData]);
  const confirm = useCallback(
    options => confirmationQueueRef.current.ask(options),
    [],
  );
  const resolveConfirm = useCallback((answer, id) => {
    confirmationQueueRef.current.resolve(answer, id);
  }, []);
  useEffect(() => () => confirmationQueueRef.current.cancelAll(), []);
  useEffect(() => {
    reload().catch((error) => notify(error.message, "error"));
  }, [reload, notify]);
  // The native window becomes visible before the temporary configuration is
  // adopted. The first full state response can therefore legitimately say
  // "waiting" or "starting". Poll only the lightweight lifecycle endpoint
  // until that transition reaches a terminal state so Settings never keeps a
  // stale "未应用" badge after the backend is already ready.
  useEffect(() => {
    const session = data?.configurationSession;
    if (!data || session?.active || session?.status === "error") return undefined;
    let stopped = false;
    let inFlight = false;
    const syncLifecycle = async () => {
      if (stopped || inFlight) return;
      inFlight = true;
      try {
        const result = await api("/api/app-lifecycle");
        if (stopped) return;
        if (result.configurationSession?.active) {
          maintenanceResourceCache = null;
          await reload();
          return;
        }
        updateData((current) => {
          if (!current) return current;
          return {
            ...current,
            configurationSession:
              result.configurationSession || current.configurationSession,
            trayStatus: result.trayStatus || current.trayStatus,
          };
        });
      } catch {
        // Startup handoffs briefly replace the local server. The next poll or
        // the new WebView bootstrap remains authoritative.
      } finally {
        inFlight = false;
      }
    };
    const timer = window.setInterval(syncLifecycle, 900);
    syncLifecycle();
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [
    Boolean(data),
    data?.configurationSession?.active,
    data?.configurationSession?.status,
    updateData,
    reload,
  ]);
  // Radar, skill catalog and repair scans are intentionally lazy. Starting
  // them beside /api/state made opening Codex and the first navigation compete
  // with several network and filesystem scans that the dashboard does not use.
  useEffect(() => {
    const timer = window.setTimeout(
      () => refreshStartupResourcesOnce().catch(() => {}),
      8000,
    );
    return () => window.clearTimeout(timer);
  }, []);
  // Keep quota and refresh results current without replacing the full root
  // state. A full /api/state reload resets open cards and also scans model,
  // history and diagnostics data that the account page does not need.
  useEffect(() => {
    if (!data || workspace !== "codex" || view !== "accounts") return undefined;
    let stopped = false;
    let inFlight = false;
    const sync = async () => {
      if (stopped || inFlight) return;
      inFlight = true;
      try {
        await refreshAccountSnapshot();
      } catch {
        // The normal action-level error handling remains authoritative. A
        // transient background read should not cover the account cards.
      } finally {
        inFlight = false;
      }
    };
    const active = ["queued", "running"].includes(data.accountRefreshStatus?.status);
    const timer = window.setInterval(sync, active ? 1600 : 15_000);
    const onVisible = () => {
      if (document.visibilityState === "visible") sync();
    };
    window.addEventListener("focus", sync);
    document.addEventListener("visibilitychange", onVisible);
    sync();
    return () => {
      stopped = true;
      window.clearInterval(timer);
      window.removeEventListener("focus", sync);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [
    Boolean(data),
    data?.accountRefreshStatus?.status,
    refreshAccountSnapshot,
    view,
    workspace,
  ]);
  const nav = useMemo(
    () => [
      { id: "accounts", label: "仪表盘", hint: "账号与服务", Icon: CircleGauge },
      { id: "routing", label: "调度中心", hint: "模型与子代理", Icon: Route },
      { id: "toolbox", label: "工具箱", hint: "2FA、邮箱与技能", Icon: Wrench },
      { id: "settings", label: "设置", hint: "运行与托盘", Icon: Settings },
    ],
    [],
  );
  const confirmDraftExit = useCallback(async () => {
    const draft = orchestrationDraftRef.current;
    if (!draft?.dirty) return true;
    const decision = await confirm({
      choiceMode: true,
      tone: "warning",
      eyebrow: "未保存的调度草稿",
      title: "离开调度中心前保存修改？",
      message: "模型选择、调度策略或高级设置已有修改但尚未同步。",
      detail: "选择保存并同步、放弃本次修改，或取消并继续编辑。",
      confirmLabel: "保存并同步",
      discardLabel: "放弃修改",
    });
    if (decision === "cancel") return false;
    if (decision === "save") return Boolean(await draft.save());
    draft.discard?.();
    return true;
  }, [confirm]);
  const changeView = async (nextView) => {
    if (nextView === view) return;
    if (view === "routing" && !(await confirmDraftExit())) return;
    setView(nextView);
    window.scrollTo({ top: 0, behavior: "auto" });
    if (nextView === "routing") {
      // The current state is already renderable. Synchronize in the
      // background so a slow account/provider probe never blocks the page
      // transition itself.
      reload().catch((error) => notify(error.message, "error"));
    }
  };
  const returnHome = async () => {
    if (view === "routing" && !(await confirmDraftExit())) return;
    setWorkspace("home");
  };
  if (!data)
    return (
      <div className="app-loading">
        <img src="/app-icon.png" alt="" />
        <Loader2 className="spin" size={22} />
        <span>正在读取账号与模型…</span>
      </div>
    );
  if (workspace === "home")
    return (
      <>
        <ProductHome onOpenCodex={() => setWorkspace("codex")} onOpenClaude={() => setWorkspace("claude")} />
        <Toast toast={toast} onClose={() => setToast(null)} />
        <ConfirmDialog dialog={confirmDialog} onResolve={resolveConfirm} />
      </>
    );
  if (workspace === "claude")
    return (
      <>
        <ClaudeWorkspace onBack={() => setWorkspace("home")} notify={notify} confirm={confirm} />
        <Toast toast={toast} onClose={() => setToast(null)} />
        <ConfirmDialog dialog={confirmDialog} onResolve={resolveConfirm} />
      </>
    );
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button
          className="brand"
          onClick={returnHome}
          title="返回 Agent Manager 首页"
        >
          <img src="/app-icon.png" alt="" />
          <div>
            <strong>Agent</strong>
            <span>Manager</span>
          </div>
        </button>
        <nav>
          {nav.map(({ id, label, hint, Icon }) => (
            <button
              key={id}
              className={view === id ? "active" : ""}
              onClick={() => changeView(id)}
            >
              <span>
                <Icon size={19} />
              </span>
              <div>
                <strong>{label}</strong>
                <small>{hint}</small>
              </div>
              {view === id && <i />}
            </button>
          ))}
        </nav>
        <div className="sidebar-status">
          <span className={data.web2apiStatus.running ? "online" : ""} />
          <div>
            <strong>
              {data.web2apiStatus.running ? "聚合服务运行中" : "本地模式"}
            </strong>
            <small>
              {data.settings.accounts.length} 个 Codex 账号 ·{" "}
              {
                data.settings.providers.filter((item) => item.kind === "custom")
                  .length
              }{" "}
              个 API
            </small>
          </div>
        </div>
      </aside>
      <main id="main-content">
        {view === "accounts" && (
          <AccountsView
            data={data}
            reload={reload}
            reloadConnections={reloadConnections}
            updateData={updateData}
            notify={notify}
            confirm={confirm}
            setBusy={setBusy}
            busy={busy}
          />
        )}
        {view === "routing" && (
          <OrchestrationView
            data={data}
            reload={reload}
            notify={notify}
            confirm={confirm}
            setBusy={setBusy}
            busy={busy}
            onDraftStateChange={(state) => {
              orchestrationDraftRef.current = state;
            }}
            sessionHistory={sessionHistory}
            onSessionHistoryChange={setSessionHistory}
          />
        )}
        {view === "toolbox" && (
          <ToolboxView notify={notify} confirm={confirm} data={data} updateData={updateData} />
        )}
        {view === "settings" && (
          <SettingsView
            data={data}
            reload={reload}
            notify={notify}
            confirm={confirm}
            setBusy={setBusy}
            busy={busy}
          />
        )}
      </main>
      <Toast toast={toast} onClose={() => setToast(null)} />
      <ConfirmDialog dialog={confirmDialog} onResolve={resolveConfirm} />
    </div>
  );
}
