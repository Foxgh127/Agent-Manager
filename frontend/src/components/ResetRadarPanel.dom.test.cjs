const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { JSDOM } = require('jsdom');

test('reset history only shows verified occurrence dates in descending order', async () => {
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
    verifiedResetHistory: ['2026-09-03','2026-09-07','2026-09-04'].map(date => ({
      completed:true, verification:'official_source', resetType:date === '2026-09-07' ? 'full-reset' : 'reset-card',
      occurredAt:date,occurrencePrecision:'date',sourceLabel:'OpenAI 帮助中心',titleZh:'官方记载',
      url:'https://help.openai.com/en/articles/20001498-how-banked-codex-resets-work',
    })),
  } }));
  const document = new JSDOM(html).window.document;
  const text = document.body.textContent;
  assert.doesNotMatch(text, /月度统计|说明文字记为|帖子发布|九月重置卡统计/);
  const entries = [...document.querySelectorAll('.reset-radar-event-history li')].map(e => e.textContent);
  assert.equal(entries.length,3);
  assert.match(entries[0],/硬重置.*2026-09-07/);
  assert.match(entries[1],/重置卡.*2026-09-04/);
  assert.match(entries[2],/重置卡.*2026-09-03/);
  assert.doesNotMatch(text, /本机首次发现|发现时间未记录|08:00—23:00|整点监测/);
  assert.match(text, /每小时后台监测/);
  assert.match(text, /综合证据分90 分/);
  assert.match(text, /公开完成表述/);
});
