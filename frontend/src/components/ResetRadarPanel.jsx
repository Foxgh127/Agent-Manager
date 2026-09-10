import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  ExternalLink,
  Loader2,
  Radio,
  RefreshCw,
} from "lucide-react";
import {
  buildResetRadarViewModel,
  formatRadarDate,
  formatResetOccurrence,
  normalizeRadarScore,
  readLocalizedField,
  safeRadarUrl,
  translationSourceLabel,
} from "../radarViewModel.js";
import "./reset-radar.css";

function hasChinese(value) {
  return /[\u3400-\u9fff]/.test(String(value || ""));
}

function plainText(value, fallback = "") {
  if (value === undefined || value === null || value === "") return fallback;
  return String(value);
}

function fieldCopy(record, field, fallback) {
  const localized = readLocalizedField(record, field);
  if (localized.text) return { ...localized, display: localized.text, language: "zh-CN" };
  if (localized.original) return { ...localized, display: localized.original, language: "en" };
  return { ...localized, display: fallback, language: "zh-CN" };
}

function FieldText({ record, field, label, as: Element = "p", className = "" }) {
  const copy = fieldCopy(record, field, `暂无${label}`);
  return (
    <div className={`reset-radar-field ${copy.needsRetry ? "is-missing" : ""} ${className}`.trim()}>
      {copy.needsRetry && <small>中文{label}待同步</small>}
      <Element lang={copy.language}>{copy.display}</Element>
    </div>
  );
}

function LocalizedInline({ record, field, fallback = "公开信息" }) {
  const copy = fieldCopy(record, field, fallback);
  return <span lang={copy.language}>{copy.display}</span>;
}

function percentageText(value) {
  if (value === null) return "—";
  return `${value.toFixed(value % 1 ? 1 : 0)} 分`;
}

function scoreText(value) {
  const score = normalizeRadarScore(value);
  return score === null ? "—" : `${score.toFixed(score % 1 ? 1 : 0)} 分`;
}

function Notice({ state }) {
  const error = plainText(state?.error);
  const message = error
    ? `刷新失败：${error}。当前保留上次有效结果。`
    : state?.refreshSuppressed
      ? "数据源暂不允许再次刷新，当前保留最近一次有效结果。"
      : state?.stale
        ? "当前内容来自上次有效结果，可稍后手动刷新。"
        : plainText(state?.feedback?.text);
  if (!message) return null;
  return (
    <div className={`reset-radar-notice ${error ? "is-error" : ""}`} role="status" aria-live="polite">
      <AlertTriangle size={16} aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}

function TranslationSource({ signal }) {
  const fields = ["title", "context", "reason"]
    .map((field) => readLocalizedField(signal, field))
    .filter((field) => field.text && field.source);
  const labels = [...new Set(fields.map((field) => translationSourceLabel(field.source)))];
  if (!labels.length) return null;
  return <small className="reset-radar-translation-source">译文：{labels.join("、")}</small>;
}

function AlertLocalizedValue({ value, label }) {
  if (value.text) return <span lang="zh-CN">{value.text}</span>;
  if (!value.original) return <span>来源未提供{label}</span>;
  return (
    <div className="reset-radar-alert-pending">
      <span>中文{label}待同步</span>
      <details>
        <summary>查看{label}原文</summary>
        <p lang={hasChinese(value.original) ? "zh-CN" : "en"}>{value.original}</p>
      </details>
    </div>
  );
}

function OriginalFields({ signal, open }) {
  const fields = [
    ["title", "标题"],
    ["context", "正文"],
    ["reason", "原因"],
  ].map(([field, label]) => ({ field, label, ...readLocalizedField(signal, field) }))
    .filter((field) => field.original);
  if (!fields.length) return null;
  return (
    <details className="reset-radar-original" open={open || undefined}>
      <summary>{open ? "中文未完整同步，核对来源原文" : "查看来源原文"}</summary>
      <dl>
        {fields.map((field) => (
          <div key={field.field}>
            <dt>{field.label}</dt>
            <dd lang={hasChinese(field.original) ? "zh-CN" : "en"}>{field.original}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function EvidenceDetails({ breakdown }) {
  if (!breakdown.length) return null;
  return (
    <details className="reset-radar-details">
      <summary>
        <span>查看判断依据</span>
        <small>{breakdown.length} 项公开信号</small>
      </summary>
      <ul>
        {breakdown.map((item, index) => {
          const points = Number(item?.points);
          return (
            <li key={`${item?.label || "basis"}-${index}`}>
              <LocalizedInline record={item} field="label" fallback="未命名依据" />
              <strong className={Number.isFinite(points) && points < 0 ? "is-negative" : ""}>
                {Number.isFinite(points) ? `${points > 0 ? "+" : ""}${points}` : "—"}
              </strong>
            </li>
          );
        })}
      </ul>
    </details>
  );
}

function ScoreHistoryDetails({ entries, formatDate }) {
  if (!entries.length) return null;
  const latest = entries[0];
  return (
    <details className="reset-radar-details reset-radar-history">
      <summary>
        <span>查看近 7 天分数变化</span>
        <small>{scoreText(latest?.fromScore)} → {scoreText(latest?.toScore)}</small>
      </summary>
      <ol>
        {entries.map((entry, index) => (
          <li key={`${entry?.at || "history"}-${index}`}>
            <header>
              <time dateTime={plainText(entry?.at)}>{formatRadarDate(entry?.at, formatDate)}</time>
              <span>{scoreText(entry?.fromScore)} → {scoreText(entry?.toScore)}</span>
            </header>
            <div>
              {(Array.isArray(entry?.changes) ? entry.changes : []).slice(0, 8).map((change, changeIndex) => {
                const delta = Number(change?.delta);
                return (
                  <article key={`${change?.label || "change"}-${changeIndex}`}>
                    <strong className={Number.isFinite(delta) && delta < 0 ? "is-negative" : ""}>
                      {Number.isFinite(delta) ? `${delta > 0 ? "+" : ""}${delta}` : "—"}
                    </strong>
                    <div>
                      <LocalizedInline record={change} field="label" fallback="分数调整" />
                      {(Array.isArray(change?.details) ? change.details : []).slice(0, 4).map((detail, detailIndex) => {
                        const sourceUrl = safeRadarUrl(detail?.url);
                        return (
                          <p key={`${detail?.name || detail?.action || "detail"}-${detailIndex}`}>
                            <LocalizedInline record={detail} field="action" fallback="依据" />：
                            <LocalizedInline record={detail} field="name" fallback="来源信息" />
                            {sourceUrl && (
                              <a href={sourceUrl} target="_blank" rel="noreferrer">
                                查看来源<span className="visually-hidden">（在新窗口打开）</span>
                                <ExternalLink size={12} aria-hidden="true" />
                              </a>
                            )}
                          </p>
                        );
                      })}
                    </div>
                  </article>
                );
              })}
            </div>
          </li>
        ))}
      </ol>
    </details>
  );
}

function ResetEventHistory({ entries }) {
  if (!entries.length) return null;
  return (
    <section className="reset-radar-event-history" aria-labelledby="reset-event-history-title">
      <header><h4 id="reset-event-history-title">已核实的重置记录</h4><span>{entries.length} 条</span></header>
      <p>仅列官方明确记载的发生日期，按来源日期倒序。来源未注明时区，不换算为北京时间；记录不代表完整历史。</p>
      {(
        <ol>{entries.map((event, index) => {
          const url = safeRadarUrl(event.url);
          return <li key={event.eventId || `${event.publishedAt}-${index}`}>
            <div className="reset-radar-event-type">
              <strong>{event.resetType === "reset-card" ? "重置卡" : "硬重置"}</strong>
              <small>{event.sourceLabel || "官方来源"}</small>
            </div>
            <h5><LocalizedInline record={event} field="title" fallback="公开重置事件" /></h5>
            <dl>
              <div><dt>发生时间</dt><dd>{formatResetOccurrence(event)}</dd></div>
            </dl>
            {url && <a href={url} target="_blank" rel="noreferrer">核对来源<ExternalLink size={12} aria-hidden="true" /></a>}
          </li>;
        })}</ol>
      )}
    </section>
  );
}

/**
 * Props contract:
 * - reset: flattened reset radar data (`forecastSignals`, `predictionSource`, `monitor`).
 * - sectionState: optional stale/error/source/confidence/updatedAt metadata.
 * - onRefresh: refreshes the reset section and missing translations.
 * - onCheckNow: optional immediate monitor check. Omit to hide the action.
 * - monitoringEnabled/onToggleMonitoring: optional persisted monitor switch.
 * - formatDate: optional application date formatter.
 */
export default function ResetRadarPanel({
  reset = {},
  sectionState = {},
  refreshing = false,
  checking = false,
  monitoringEnabled = true,
  onRefresh,
  onCheckNow,
  onToggleMonitoring,
  onRetryTranslation,
  formatDate,
  sourceUrl = "https://codexradar.com/",
}) {
  const model = buildResetRadarViewModel(reset);
  const { alert, latestSignal, monitor } = model;
  const busy = refreshing || checking;
  const latestUrl = safeRadarUrl(latestSignal?.url);
  const publicSourceUrl = safeRadarUrl(sourceUrl);
  const sourceUrls = (Array.isArray(alert?.sourceUrls) ? alert.sourceUrls : [])
    .map(safeRadarUrl)
    .filter(Boolean);
  const delivery = monitor.alertDeliveries?.[alert?.signature];
  const retryTranslation = onRetryTranslation || onRefresh;

  return (
    <article className="radar-card reset reset-radar-panel" aria-labelledby="reset-radar-title">
      <header className="reset-radar-header">
        <span className="radar-icon"><Radio size={20} aria-hidden="true" /></span>
        <div>
          <small>PUBLIC RESET SIGNALS</small>
          <h3 id="reset-radar-title">重置雷达</h3>
        </div>
        <button
          type="button"
          className="icon-plain"
          onClick={onRefresh}
          disabled={busy || typeof onRefresh !== "function"}
          aria-label="手动刷新重置雷达和中文译文"
        >
          <RefreshCw className={refreshing ? "spin" : ""} size={17} aria-hidden="true" />
        </button>
      </header>

      <Notice state={sectionState} />

      <section className={`reset-radar-verdict tone-${model.judgmentTone}`} aria-labelledby="reset-radar-verdict-title">
        <div className="reset-radar-verdict-copy">
          <small>当前判断</small>
          <h4 id="reset-radar-verdict-title">{model.judgment}</h4>
          <p>{model.judgmentDetail}</p>
          {alert && (
            <dl className="reset-radar-alert-facts">
              <div>
                <dt>公开证据</dt>
                <dd><AlertLocalizedValue value={model.alertEvidence} label="证据" /></dd>
              </div>
              <div>
                <dt>时间窗口</dt>
                <dd>{formatRadarDate(alert.window, formatDate)}</dd>
              </div>
              {(model.alertAdvice.text || model.alertAdvice.original) && (
                <div>
                  <dt>来源建议</dt>
                  <dd><AlertLocalizedValue value={model.alertAdvice} label="建议" /></dd>
                </div>
              )}
            </dl>
          )}
        </div>
        <div className="reset-radar-score" aria-label={`综合证据分 ${percentageText(model.assessment?.score ?? model.score)}`}>
          <span>{model.assessment ? "综合证据分" : "社区信号分"}</span>
          <strong>{percentageText(model.assessment?.score ?? model.score)}</strong>
          <i aria-hidden="true"><b style={{ width: `${model.assessment?.score ?? model.score ?? 0}%` }} /></i>
          <small>{model.assessment ? `${model.assessment.label}级 · 非发生概率` : "满分 100，非发生概率或官方承诺"}</small>
          {model.assessment && model.score !== null && <small>第三方原始分：{percentageText(model.score)}</small>}
        </div>
        <div className="reset-radar-next">
          <small>下一步</small>
          <strong>{model.action}</strong>
          {sourceUrls.length > 0 && (
            <a href={sourceUrls[0]} target="_blank" rel="noreferrer">
              核对预警来源<span className="visually-hidden">（在新窗口打开）</span>
              <ExternalLink size={13} aria-hidden="true" />
            </a>
          )}
          {model.alertTranslationNeedsRetry && typeof retryTranslation === "function" && (
            <button type="button" className="reset-radar-inline-retry" onClick={retryTranslation} disabled={busy}>
              <RefreshCw className={refreshing ? "spin" : ""} size={13} aria-hidden="true" />
              重试警报译文
            </button>
          )}
        </div>
      </section>

      {(typeof onToggleMonitoring === "function" || monitoringEnabled || monitor.lastSuccessAt || monitor.lastRunAt || monitor.lastAlert) && <section className="reset-radar-monitor" aria-labelledby="reset-radar-monitor-title">
        <div className="reset-radar-monitor-heading">
          <Clock3 size={17} aria-hidden="true" />
          <div>
            <h4 id="reset-radar-monitor-title">{monitoringEnabled ? "每小时后台监测" : "检查记录"}</h4>
            <p>{monitoringEnabled ? "启动立即检查，运行期间每小时更新，全天有效" : "当前按需刷新，不会在后台周期轮询。"}</p>
          </div>
          {typeof onToggleMonitoring === "function" && (
            <label className="reset-radar-switch">
              <input
                type="checkbox"
                checked={Boolean(monitoringEnabled)}
                onChange={(event) => onToggleMonitoring(event.target.checked)}
                disabled={busy}
              />
              <span>{monitoringEnabled ? "已开启" : "已暂停"}</span>
            </label>
          )}
        </div>
        <div className="reset-radar-monitor-status" role="status" aria-live="polite">
          {monitor.lastError
            || model.checkStale ? <AlertTriangle className="is-warning" size={16} aria-hidden="true" />
            : <CheckCircle2 size={16} aria-hidden="true" />}
          <strong>{model.monitorResult}</strong>
          <span>最近检查 {formatRadarDate(monitor.lastRunAt || monitor.lastSuccessAt, formatDate)}</span>
          <span>{monitor.lastCheckMode === "manual-refresh" ? "手动刷新评估" : monitor.lastCheckMode === "manual-check" ? "手动立即检查" : "后台检查"}</span>
          {monitoringEnabled && <span>下次 {formatRadarDate(monitor.nextCheckAt, formatDate)}</span>}
        </div>
        {monitoringEnabled && <p className="reset-radar-monitor-note">
          新信号或等级提升时发送 Windows 通知；同一信号不会反复提醒。
        </p>}
        {!monitoringEnabled && <p className="reset-radar-monitor-note">手动刷新会评估当前来源并更新检查记录；后台关闭期间不会主动检查或推送通知。</p>}
        {delivery?.state === "pending" && delivery.attempts > 0 && <p className="reset-radar-monitor-note">Windows 通知待重试，已尝试 {delivery.attempts}/3 次。</p>}
        {delivery?.state === "failed" && <p className="reset-radar-monitor-note">Windows 通知提交失败，请检查系统通知设置。</p>}
        {typeof onCheckNow === "function" && (
          <button type="button" className="reset-radar-action" onClick={onCheckNow} disabled={busy}>
            {checking ? <Loader2 className="spin" size={15} aria-hidden="true" /> : <RefreshCw size={15} aria-hidden="true" />}
            立即检查
          </button>
        )}
      </section>}

      <section className="reset-radar-signal" aria-labelledby="reset-radar-signal-title">
        <header>
          <div>
            <small>最新公开信号</small>
            <span>{plainText(latestSignal?.sourceLabel, "@thsottiaux 公开动态")}</span>
          </div>
          {latestUrl && (
            <a href={latestUrl} target="_blank" rel="noreferrer">
              查看来源<span className="visually-hidden">（在新窗口打开）</span>
              <ExternalLink size={13} aria-hidden="true" />
            </a>
          )}
        </header>
        {latestSignal ? (
          <>
            <FieldText record={latestSignal} field="title" label="标题" as="h4" className="is-title" />
            {(latestSignal.context || latestSignal.contextZh || latestSignal.translated?.context) && (
              <FieldText record={latestSignal} field="context" label="正文" />
            )}
            {(latestSignal.reason || latestSignal.reasonZh || latestSignal.translated?.reason) && (
              <div className="reset-radar-reason">
                <strong>为何计入</strong>
                <FieldText record={latestSignal} field="reason" label="原因" />
              </div>
            )}
            <TranslationSource signal={latestSignal} />
            <OriginalFields signal={latestSignal} open={model.translationNeedsRetry} />
            {model.translationNeedsRetry && typeof retryTranslation === "function" && (
              <button type="button" className="reset-radar-retry" onClick={retryTranslation} disabled={busy}>
                <RefreshCw className={refreshing ? "spin" : ""} size={14} aria-hidden="true" />
                重试中文译文
              </button>
            )}
          </>
        ) : (
          <p className="reset-radar-empty">暂无新的公开信号。</p>
        )}
      </section>

      <div className="reset-radar-disclosures">
        <EvidenceDetails breakdown={model.breakdown} />
        <ScoreHistoryDetails entries={model.scoreHistory} formatDate={formatDate} />
      </div>

      <ResetEventHistory entries={model.resetHistory} />

      <footer className="reset-radar-footer">
        <span>来源：{plainText(sectionState.source, "OpenAI 公开活动与社区预测")}</span>
        <time>更新：{formatRadarDate(sectionState.updatedAt, formatDate)}</time>
        {publicSourceUrl && (
          <a href={publicSourceUrl} target="_blank" rel="noreferrer">
            Codex Radar<span className="visually-hidden">（在新窗口打开）</span>
            <ExternalLink size={12} aria-hidden="true" />
          </a>
        )}
      </footer>
    </article>
  );
}
