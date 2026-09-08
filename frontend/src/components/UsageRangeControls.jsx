import { useId } from "react";
import {
  normalizeUsageRange,
  resolveUsageRange,
  serializeUsageRangePreference,
} from "../usageRange.js";
import "./UsageRangeControls.css";

const RANGE_OPTIONS = [
  { mode: "today", label: "今日" },
  { mode: "last7", label: "近 7 天" },
  { mode: "month", label: "本月" },
  { mode: "custom", label: "自定义" },
];

export function UsageRangeControls({
  value,
  onChange,
  onCommit,
  now = new Date(),
  disabled = false,
  className = "",
}) {
  const id = useId();
  const normalized = normalizeUsageRange(value);
  const resolved = resolveUsageRange(normalized, now);
  const errorId = `${id}-error`;

  const update = (patch) => {
    const next = normalizeUsageRange({ ...normalized, ...patch });
    if (next.mode === normalized.mode && next.customStart === normalized.customStart && next.customEnd === normalized.customEnd) return;
    onChange?.(next);
    const nextResolved = resolveUsageRange(next, now);
    if (nextResolved.valid) {
      onCommit?.(serializeUsageRangePreference(next, now), nextResolved);
    }
  };

  return (
    <div className={`usage-range-control ${className}`.trim()}>
      <div className="usage-range-presets" role="group" aria-label="统计时间范围">
        {RANGE_OPTIONS.map((option) => (
          <button
            key={option.mode}
            type="button"
            aria-pressed={normalized.mode === option.mode}
            disabled={disabled}
            onClick={() => update({ mode: option.mode })}
          >
            {option.label}
          </button>
        ))}
      </div>
      {normalized.mode === "custom" && (
        <div className="usage-range-custom" aria-describedby={!resolved.valid ? errorId : undefined}>
          <label htmlFor={`${id}-start`}>
            <span>开始日期</span>
            <input
              id={`${id}-start`}
              type="date"
              value={normalized.customStart}
              max={normalized.customEnd || undefined}
              disabled={disabled}
              aria-invalid={!resolved.valid || undefined}
              onInput={(event) => update({ mode: "custom", customStart: event.currentTarget.value })}
              onChange={(event) => update({ mode: "custom", customStart: event.target.value })}
            />
          </label>
          <span aria-hidden="true">至</span>
          <label htmlFor={`${id}-end`}>
            <span>结束日期</span>
            <input
              id={`${id}-end`}
              type="date"
              value={normalized.customEnd}
              min={normalized.customStart || undefined}
              disabled={disabled}
              aria-invalid={!resolved.valid || undefined}
              onInput={(event) => update({ mode: "custom", customEnd: event.currentTarget.value })}
              onChange={(event) => update({ mode: "custom", customEnd: event.target.value })}
            />
          </label>
        </div>
      )}
      {!resolved.valid && (
        <p className="usage-range-error" id={errorId} role="alert">{resolved.error}</p>
      )}
    </div>
  );
}
