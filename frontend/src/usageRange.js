export const USAGE_RANGE_PREFERENCE_KEY = "usageRange";
export const USAGE_RANGE_PREFERENCE_PATH = "appBehavior.usageRange";
export const USAGE_RANGE_SCHEMA_VERSION = 1;
export const USAGE_RANGE_MODES = Object.freeze(["today", "last7", "month", "custom"]);
export const DEFAULT_USAGE_RANGE = Object.freeze({
  schemaVersion: USAGE_RANGE_SCHEMA_VERSION,
  mode: "last7",
  customStart: "",
  customEnd: "",
});

const DATE_KEY_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;
const BEIJING_TIME_ZONE = "Asia/Shanghai";

function dateParts(date, timeZone = BEIJING_TIME_ZONE) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  return Object.fromEntries(parts.map((part) => [part.type, part.value]));
}

export function isUsageDateKey(value) {
  const match = DATE_KEY_PATTERN.exec(String(value || ""));
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day;
}

export function beijingDateKey(now = new Date()) {
  const date = now instanceof Date ? now : new Date(now);
  if (!Number.isFinite(date.getTime())) throw new TypeError("now 必须是有效日期");
  const parts = dateParts(date);
  return `${parts.year}-${parts.month}-${parts.day}`;
}

export function addUsageDays(dateKey, offset) {
  if (!isUsageDateKey(dateKey) || !Number.isInteger(offset)) {
    throw new TypeError("日期必须为 YYYY-MM-DD，偏移必须为整数");
  }
  const [year, month, day] = dateKey.split("-").map(Number);
  const result = new Date(Date.UTC(year, month - 1, day + offset));
  return [
    String(result.getUTCFullYear()).padStart(4, "0"),
    String(result.getUTCMonth() + 1).padStart(2, "0"),
    String(result.getUTCDate()).padStart(2, "0"),
  ].join("-");
}

export function normalizeUsageRange(value) {
  const source = value && typeof value === "object" ? value : {};
  const mode = USAGE_RANGE_MODES.includes(source.mode) ? source.mode : DEFAULT_USAGE_RANGE.mode;
  const customStart = String(source.customStart ?? source.startDate ?? "").trim();
  const customEnd = String(source.customEnd ?? source.endDate ?? "").trim();
  return {
    schemaVersion: USAGE_RANGE_SCHEMA_VERSION,
    mode,
    customStart,
    customEnd,
  };
}

export function resolveUsageRange(value, now = new Date()) {
  const range = normalizeUsageRange(value);
  const today = beijingDateKey(now);
  let startDate = today;
  let endDate = today;
  let error = "";

  if (range.mode === "last7") startDate = addUsageDays(today, -6);
  else if (range.mode === "month") startDate = `${today.slice(0, 7)}-01`;
  else if (range.mode === "custom") {
    startDate = range.customStart;
    endDate = range.customEnd;
    if (!isUsageDateKey(startDate) || !isUsageDateKey(endDate)) {
      error = "请选择有效的开始和结束日期。";
    } else if (startDate > endDate) {
      error = "开始日期不能晚于结束日期。";
    }
  }

  return {
    ...range,
    startDate,
    endDate,
    valid: !error,
    error,
  };
}

export function serializeUsageRangePreference(value, now = new Date()) {
  const resolved = resolveUsageRange(value, now);
  if (!resolved.valid) throw new TypeError(resolved.error);
  return {
    schemaVersion: USAGE_RANGE_SCHEMA_VERSION,
    mode: resolved.mode,
    customStart: isUsageDateKey(resolved.customStart) ? resolved.customStart : null,
    customEnd: isUsageDateKey(resolved.customEnd) ? resolved.customEnd : null,
  };
}

export function usageRecordDateKey(record) {
  if (!record || typeof record !== "object") return "";
  for (const field of ["date", "day"]) {
    const direct = String(record[field] || "").slice(0, 10);
    if (isUsageDateKey(direct)) return direct;
  }
  for (const field of ["timestamp", "lastSeenAt", "createdAt", "updatedAt"]) {
    const raw = record[field];
    if (!raw) continue;
    const parsed = new Date(raw);
    if (Number.isFinite(parsed.getTime())) return beijingDateKey(parsed);
  }
  return "";
}

export function recordMatchesUsageRange(record, value, now = new Date()) {
  const resolved = resolveUsageRange(value, now);
  if (!resolved.valid) return false;
  const date = usageRecordDateKey(record);
  return Boolean(date && date >= resolved.startDate && date <= resolved.endDate);
}

export function filterUsageRecordsByRange(records, value, now = new Date()) {
  if (!Array.isArray(records)) return [];
  const resolved = resolveUsageRange(value, now);
  if (!resolved.valid) return [];
  return records.filter((record) => {
    const date = usageRecordDateKey(record);
    return date && date >= resolved.startDate && date <= resolved.endDate;
  });
}
