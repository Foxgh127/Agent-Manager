const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { JSDOM } = require('jsdom');

test('reset history shows source precision/conflicts without local discovery times', async () => {
  const { transformWithOxc } = await import('vite');
  const source = fs.readFileSync(path.join(__dirname, 'ResetRadarPanel.jsx'), 'utf8')
    .replace(/^import\s+[\s\S]*?from\s+["'][^"']+["'];\s*/gm, '')
    .replace(/^import\s+["'][^"']+["'];\s*/gm, '')
    .replace('export default function', 'function');
  const transformed = await transformWithOxc(source, 'ResetRadarPanel.jsx', { jsx: { runtime: 'classic' } });
  const now = new Date().toISOString();
  const context = vm.createContext({ React, Date, Intl, ...await import('../radarViewModel.js'),
    ...Object.fromEntries(['AlertTriangle','CheckCircle2','Clock3','ExternalLink','Loader2','Radio','RefreshCw'].map(key => [key, () => null])) });
  vm.runInContext(transformed.code + '\nthis.Panel=ResetRadarPanel;', context);
  const html = renderToStaticMarkup(React.createElement(context.Panel, { reset: {
    assessment: { severity: 'confirmed', label: '确认', score: 90 },
    monitor: { lastRunAt: now, lastAlert: { level: 'C', detectedAt: now, evidenceZh: '公开完成表述', adviceZh: '核对账号' } },
    resetHistory: [{ aggregate: true, resetType: 'reset-card', occurredAt: '2026-09', occurrencePrecision: 'month',
      count: 3, reportedCount: 3, noteCount: 2, discrepancy: true, titleZh: '九月重置卡统计',
      discoveredAt: now, url: 'https://codexradar.com/' }],
  } }));
  const document = new JSDOM(html).window.document;
  const text = document.body.textContent;
  assert.match(text, /月度统计 3 次/);
  assert.match(text, /说明文字记为 2 次/);
  assert.match(text, /2026-09（月度汇总/);
  assert.doesNotMatch(text, /本机首次发现|发现时间未记录|08:00—23:00|整点监测/);
  assert.match(text, /每小时后台监测/);
  assert.match(text, /综合证据分90 分/);
  assert.match(text, /公开完成表述/);
});
