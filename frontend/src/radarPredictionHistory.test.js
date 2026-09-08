import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildResetRadarViewModel, formatResetOccurrence, isCurrentRadarAlert } from './radarViewModel.js';

const now = Date.parse('2026-09-08T13:00:00Z');
test('fresh high community score has an explicit uncertain prediction warning', () => {
  const model = buildResetRadarViewModel({ predictionSource: { score: 100 }, forecastSignals: { checkedAt: '2026-09-08T12:55:00Z' } }, { now });
  assert.equal(model.judgment, '社区预测预警');
  assert.match(model.judgmentDetail, /不表示.*必定发生/);
});
test('P alerts expire and old monitor timestamps are visibly stale', () => {
  assert.equal(isCurrentRadarAlert({ level: 'P', detectedAt: '2026-09-08T12:00:00Z', expiresAt: '2026-09-08T12:30:00Z' }, now), false);
  const model = buildResetRadarViewModel({ monitor: { lastRunAt: '2026-08-12T15:00:00Z', lastResult: '无预警' } }, { now });
  assert.equal(model.checkStale, true);
  assert.match(model.monitorResult, /已过期/);
});
test('publication timestamp is never substituted for an unknown occurrence', () => {
  assert.equal(formatResetOccurrence({ publishedAt: '2026-09-08T12:34:56Z', resetAt: '2026-09-08T12:34:56Z' }), '发生时间未公布');
  assert.equal(formatResetOccurrence({ occurredAt: '2026-09-08', occurrencePrecision: 'date' }), '2026-09-08（仅日期，具体时刻未公布）');
});
test('minute precision does not acquire fabricated seconds', () => {
  const value = formatResetOccurrence({ occurredAt: '2026-09-08T12:34+08:00', occurrencePrecision: 'minute' });
  assert.match(value, /12:34/);
  assert.doesNotMatch(value, /12:34:00/);
});
