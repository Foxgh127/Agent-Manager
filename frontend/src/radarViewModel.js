const CHINESE_TEXT = /[\u3400-\u9fff]/;
const ALERT_MAX_AGE_MS = 24 * 60 * 60 * 1000;
const ALERT_CLOCK_SKEW_MS = 5 * 60 * 1000;

function text(value) {
  if (value === undefined || value === null) return "";
  return String(value).replace(/\s+/g, " ").trim();
}

function boundedPercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.min(100, number)) : null;
}

export function normalizeRadarScore(value) {
  const raw = text(value);
  if (!raw) return null;
  return boundedPercent(raw.endsWith("%") ? raw.slice(0, -1) : raw);
}

export function normalizeRadarProbability(value) {
  const raw = text(value);
  if (!raw) return null;
  const isPercent = raw.endsWith("%");
  const number = Number(isPercent ? raw.slice(0, -1) : raw);
  if (!Number.isFinite(number)) return null;
  return boundedPercent(isPercent || number > 1 ? number : number * 100);
}

export function readPredictionPercent(prediction = {}) {
  if (Object.prototype.hasOwnProperty.call(prediction, "score")) {
    const score = normalizeRadarScore(prediction.score);
    if (score !== null) return score;
  }
  return normalizeRadarProbability(prediction.probability);
}

export function safeRadarUrl(value) {
  const raw = text(value);
  if (!raw) return "";
  try {
    const parsed = new URL(raw);
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) return "";
    return parsed.href;
  } catch {
    return "";
  }
}

export function readLocalizedField(record = {}, field = "title") {
  if (!record || typeof record !== "object") {
    return { text: "", original: "", state: "not-needed", source: "", needsRetry: false };
  }
  const metadata = record.translationFields?.[field] || {};
  const original = [
    metadata.original,
    field === "title" ? record.originalText : undefined,
    record[field],
  ].map(text).find(Boolean) || "";
  const candidates = [
    record[`${field}Zh`],
    record.translated?.[field],
    ...(field === "title" ? [record.translationZh, record.translatedTextZh] : []),
  ];
  let localized = candidates.map(text).find((candidate) => CHINESE_TEXT.test(candidate)) || "";
  let source = text(metadata.source || (field === "title" ? record.translationSource : ""));
  if (!localized && CHINESE_TEXT.test(original)) {
    localized = original;
    source ||= "original-zh";
  }
  const declaredState = text(metadata.state || record.translationState || "");
  const state = localized
    ? "complete"
    : declaredState === "failed"
      ? "failed"
      : original
        ? "pending"
        : "not-needed";
  return {
    text: localized,
    original,
    state,
    source,
    error: text(metadata.error || record.translationError),
    needsRetry: Boolean(original && !localized),
  };
}

export function translationSourceLabel(value) {
  return ({
    "codexradar.com": "Codex Radar 主站译文",
    "local-reviewed": "内置校对译文",
    mymemory: "公共翻译服务译文",
    "original-zh": "来源已提供中文",
  })[text(value).toLowerCase()] || "来源译文";
}

export function formatRadarDate(value, formatter) {
  const raw = text(value);
  if (!raw) return "待同步";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  if (typeof formatter === "function") return formatter(raw);
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function timestamp(value) {
  if (value instanceof Date) return value.getTime();
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const raw = text(value);
  if (!raw) return null;
  const parsed = Date.parse(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

export function isCurrentRadarAlert(alert, now = Date.now()) {
  if (!alert || typeof alert !== "object" || !["A", "B", "C", "P"].includes(text(alert.level).toUpperCase())) {
    return false;
  }
  const nowMs = timestamp(now);
  const detectedAt = timestamp(alert.detectedAt || alert.createdAt);
  if (nowMs === null || detectedAt === null) return false;
  if (detectedAt > nowMs + ALERT_CLOCK_SKEW_MS || nowMs - detectedAt > ALERT_MAX_AGE_MS) return false;
  const explicitEnd = timestamp(alert.expiresAt || alert.validUntil || alert.windowEnd);
  if (explicitEnd !== null && explicitEnd < nowMs) return false;
  const windowEnd = timestamp(alert.window);
  if (windowEnd !== null && windowEnd < nowMs) return false;
  return true;
}

export function formatResetOccurrence(event = {}) {
  const precision = text(event.occurrencePrecision);
  const raw = text(event.occurredAt);
  if (precision === "month" && /^\d{4}-(0[1-9]|1[0-2])$/.test(raw)) return `${raw}（月度汇总，具体日期未公布）`;
  if (!raw || !["date", "minute", "second"].includes(precision)) return "发生时间未公布";
  if (precision === "date") return `${raw}（仅日期，具体时刻未公布）`;
  const parsed = new Date(raw);
  if (!Number.isFinite(parsed.getTime())) return "发生时间未公布";
  return parsed.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", ...(precision === "second" ? { second: "2-digit" } : {}),
    hour12: false,
  }) + (precision === "minute" ? "（来源精确到分钟）" : "");
}

export function buildResetRadarViewModel(reset = {}, { now = Date.now() } = {}) {
  const forecast = reset.forecastSignals && typeof reset.forecastSignals === "object"
    ? reset.forecastSignals
    : {};
  const prediction = reset.predictionSource && typeof reset.predictionSource === "object"
    ? reset.predictionSource
    : forecast.predictor && typeof forecast.predictor === "object"
      ? forecast.predictor
      : {};
  const monitor = reset.monitor && typeof reset.monitor === "object" ? reset.monitor : {};
  const lastAlert = monitor.lastAlert && typeof monitor.lastAlert === "object" ? monitor.lastAlert : null;
  const alert = isCurrentRadarAlert(lastAlert, now) ? lastAlert : null;
  const expiredAlert = lastAlert && !alert ? lastAlert : null;
  const alertEvidence = alert ? readLocalizedField(alert, "evidence") : readLocalizedField({}, "evidence");
  const alertAdvice = alert ? readLocalizedField(alert, "advice") : readLocalizedField({}, "advice");
  const alertTranslationNeedsRetry = alertEvidence.needsRetry || alertAdvice.needsRetry;
  const score = readPredictionPercent(prediction);
  const sourceCheckedAt = timestamp(forecast.checkedAt);
  const sourceFresh = sourceCheckedAt !== null && Number(now) - sourceCheckedAt <= 6 * 60 * 60 * 1000
    && sourceCheckedAt <= Number(now) + ALERT_CLOCK_SKEW_MS;
  const checkAt = timestamp(monitor.lastRunAt || monitor.lastSuccessAt);
  const checkStale = checkAt !== null && Number(now) - checkAt > ALERT_MAX_AGE_MS;
  const resetHistory = (Array.isArray(reset.resetHistory) ? reset.resetHistory : [])
    .filter((entry) => entry && (entry.completed === true || entry.aggregate === true));
  const latestResetEvent = resetHistory.filter(entry => entry.resetType === "full-reset" && !entry.aggregate
    && ["date", "minute", "second"].includes(entry.occurrencePrecision) && Number.isFinite(Date.parse(entry.occurredAt)))
    .sort((a, b) => Date.parse(b.occurredAt) - Date.parse(a.occurredAt))[0] || null;
  const latestSignal = forecast.latestSignal && typeof forecast.latestSignal === "object"
    ? forecast.latestSignal
    : null;
  const cutoff = Number(now) - 7 * 24 * 60 * 60 * 1000;
  const scoreHistory = (Array.isArray(forecast.scoreHistory) ? forecast.scoreHistory : [])
    .filter((entry) => {
      const timestamp = new Date(entry?.at).getTime();
      return !Number.isFinite(timestamp) || timestamp >= cutoff;
    })
    .slice(0, 12);
  const breakdown = Array.isArray(prediction.breakdown) ? prediction.breakdown.slice(0, 12) : [];
  const translationFields = latestSignal
    ? ["title", "context", "reason"].map((field) => readLocalizedField(latestSignal, field))
    : [];
  const translationNeedsRetry = translationFields.some((field) => field.needsRetry);

  let judgment = "暂无明确重置信号";
  let judgmentTone = "neutral";
  let judgmentDetail = "继续观察公开来源；预测数值不代表 OpenAI 已承诺重置。";
  if (alert?.level === "A") {
    judgment = "发现 A 级公开信号";
    judgmentTone = "critical";
    judgmentDetail = "存在来源明确且指向未来时间窗的额度信号，请先核对原始来源。";
  } else if (alert?.level === "B") {
    judgment = "发现 B 级观察信号";
    judgmentTone = "attention";
    judgmentDetail = "信号较强，但范围或时间仍不明确，暂不能视为官方承诺。";
  } else if (alert?.level === "P" || (score !== null && score >= 70 && sourceFresh)) {
    judgment = "社区预测预警";
    judgmentTone = "attention";
    judgmentDetail = "社区评分达到预警阈值；100 分也不表示重置必定发生，尚需官方确认执行。";
  } else if (monitor.lastError) {
    judgment = "本次检查未完成";
    judgmentTone = "attention";
    judgmentDetail = "当前保留上次有效结果，可手动重试。";
  } else if (score !== null && score >= 70) {
    judgment = "社区重置信号偏强";
    judgmentTone = "attention";
    judgmentDetail = "尚无可确认重置执行的官方公开证据；来源时间不足或已过期，请刷新后再判断。";
  } else if (score !== null && score >= 40) {
    judgment = "出现部分重置信号";
    judgmentDetail = "线索仍不足以确认重置，请等待后续公开信息。";
  }

  const rawAssessment = reset.assessment || monitor.assessment;
  const assessmentScore = normalizeRadarScore(rawAssessment?.score);
  const assessment = rawAssessment && assessmentScore !== null ? { ...rawAssessment, score: assessmentScore } : null;
  if (assessment) {
    const copy = {
      confirmed: ["发现已完成的公开公告", "critical", "来源出现完成表述；具体到账情况请核对实际账号。"],
      warning: ["重置预警", "critical", "存在较新且可核对的重置计划或额度变化信号。"],
      watch: ["关注重置信号", "attention", "存在值得关注的公开线索，尚不能视为已完成或必定发生。"],
      information: ["暂无明确重置预警", "neutral", "目前缺少足够的新鲜证据，继续按小时观察。"],
    }[assessment.severity];
    if (copy && !checkStale) [judgment, judgmentTone, judgmentDetail] = copy;
  }
  const action = alertAdvice.text
    ? alertAdvice.text
    : alertAdvice.needsRetry
      ? "核对公开来源，等待中文建议"
    : translationNeedsRetry
      ? "刷新中文译文，并核对原文"
      : "继续观察，按需手动检查";

  const monitorResult = checkStale ? "检查记录已过期，请重新检查" : expiredAlert && /[ABＡＢ]\s*级|预警/i.test(text(monitor.lastResult))
    ? "最近一次预警已过期"
    : text(monitor.lastResult) || "尚未检查";

  return {
    forecast,
    prediction,
    monitor,
    monitorResult,
    checkStale,
    sourceFresh,
    resetHistory,
    latestResetEvent,
    alert,
    expiredAlert,
    alertEvidence,
    alertAdvice,
    alertTranslationNeedsRetry,
    score,
    assessment,
    latestSignal,
    scoreHistory,
    breakdown,
    translationNeedsRetry,
    judgment,
    judgmentTone,
    judgmentDetail,
    action,
  };
}
