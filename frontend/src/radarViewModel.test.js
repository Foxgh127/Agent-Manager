import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildResetRadarViewModel,
  formatRadarDate,
  isCurrentRadarAlert,
  normalizeRadarProbability,
  normalizeRadarScore,
  readLocalizedField,
  readPredictionPercent,
  safeRadarUrl,
} from "./radarViewModel.js";

test("descriptive alert windows survive application date formatting", () => {
  assert.equal(formatRadarDate("尚未确认重置时间", () => "未提供"), "尚未确认重置时间");
});

test("score uses the documented 0-100 scale while probability accepts ratios", () => {
  assert.equal(normalizeRadarScore(1), 1);
  assert.equal(normalizeRadarProbability(1), 100);
  assert.equal(normalizeRadarProbability("72%"), 72);
  assert.equal(readPredictionPercent({ score: 1, probability: 0.93 }), 1);
  assert.equal(readPredictionPercent({ probability: 0.93 }), 93);
});

test("localized fields use field-specific Chinese and never reuse a generic summary", () => {
  const record = {
    title: "Original title",
    context: "Original body",
    summaryZh: "不应冒充正文",
    titleZh: "中文标题",
    translated: { context: "中文正文" },
    translationFields: {
      title: { state: "complete", source: "mymemory", original: "Original title" },
      context: { state: "complete", source: "mymemory", original: "Original body" },
      reason: { state: "failed", original: "Original reason", error: "temporary" },
    },
  };
  assert.equal(readLocalizedField(record, "title").text, "中文标题");
  assert.equal(readLocalizedField(record, "context").text, "中文正文");
  assert.equal(readLocalizedField(record, "reason").text, "");
  assert.equal(readLocalizedField(record, "reason").original, "Original reason");
  assert.equal(readLocalizedField(record, "reason").needsRetry, true);
});

test("English copied into a zh field remains pending and exposes the original", () => {
  const field = readLocalizedField({
    title: "English source",
    titleZh: "English source",
    translationState: "complete",
  }, "title");
  assert.equal(field.text, "");
  assert.equal(field.original, "English source");
  assert.equal(field.state, "pending");
  assert.equal(field.needsRetry, true);
});

test("links allow only absolute http(s) URLs without credentials", () => {
  assert.equal(safeRadarUrl("https://x.com/openai/status/1"), "https://x.com/openai/status/1");
  assert.equal(safeRadarUrl("http://example.test/source"), "http://example.test/source");
  assert.equal(safeRadarUrl("javascript:alert(1)"), "");
  assert.equal(safeRadarUrl("https://user:secret@example.test/source"), "");
  assert.equal(safeRadarUrl("/relative"), "");
});

test("view model states high prediction as uncertain and caps history", () => {
  const model = buildResetRadarViewModel({
    predictionSource: { score: 87 },
    forecastSignals: {
      latestSignal: { title: "No translation yet", translationState: "failed" },
      scoreHistory: Array.from({ length: 20 }, (_, index) => ({
        at: `2026-09-06T00:${String(index).padStart(2, "0")}:00Z`,
      })),
    },
    monitor: {},
  }, { now: Date.UTC(2026, 8, 6) });
  assert.equal(model.score, 87);
  assert.equal(model.judgment, "社区重置信号偏强");
  assert.match(model.judgmentDetail, /尚无.*官方公开证据/);
  assert.equal(model.translationNeedsRetry, true);
  assert.equal(model.scoreHistory.length, 12);
});

test("a recent alert with an open explicit window is current", () => {
  const now = Date.parse("2026-09-06T08:00:00Z");
  const alert = {
    level: "A",
    detectedAt: "2026-09-06T07:30:00Z",
    window: "2026-09-06T12:00:00Z",
    evidence: "Official source evidence",
    evidenceZh: "官方来源证据",
    advice: "Use quota now",
    translated: { advice: "按需集中使用额度" },
  };
  const model = buildResetRadarViewModel({ monitor: { lastAlert: alert } }, { now });
  assert.equal(isCurrentRadarAlert(alert, now), true);
  assert.equal(model.alert, alert);
  assert.equal(model.judgment, "发现 A 级公开信号");
  assert.equal(model.alertEvidence.text, "官方来源证据");
  assert.equal(model.action, "按需集中使用额度");
});

test("an alert expires when its explicit window has ended", () => {
  const now = Date.parse("2026-09-06T13:00:00Z");
  const alert = {
    level: "A",
    detectedAt: "2026-09-06T11:00:00Z",
    window: "2026-09-06T12:00:00Z",
    evidenceZh: "已过期的证据",
  };
  const model = buildResetRadarViewModel({
    monitor: { lastAlert: alert, lastResult: "A级预警" },
  }, { now });
  assert.equal(isCurrentRadarAlert(alert, now), false);
  assert.equal(model.alert, null);
  assert.equal(model.expiredAlert, alert);
  assert.equal(model.judgment, "暂无明确重置信号");
  assert.equal(model.monitorResult, "最近一次预警已过期");
});

test("English alert evidence and advice stay pending instead of entering primary copy", () => {
  const now = Date.parse("2026-09-06T08:00:00Z");
  const model = buildResetRadarViewModel({ monitor: { lastAlert: {
    level: "B",
    detectedAt: "2026-09-06T07:30:00Z",
    window: "时间尚未明确（北京时间）",
    evidence: "An English evidence sentence",
    advice: "An English action sentence",
    translationFields: {
      evidence: { state: "failed", original: "An English evidence sentence" },
      advice: { state: "pending", original: "An English action sentence" },
    },
  } } }, { now });
  assert.equal(model.alertEvidence.text, "");
  assert.equal(model.alertEvidence.original, "An English evidence sentence");
  assert.equal(model.alertAdvice.text, "");
  assert.equal(model.alertTranslationNeedsRetry, true);
  assert.equal(model.action, "核对公开来源，等待中文建议");
  assert.doesNotMatch(model.action, /English/);
});
