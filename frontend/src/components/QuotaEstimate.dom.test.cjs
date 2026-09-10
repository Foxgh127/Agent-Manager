const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { JSDOM } = require('jsdom');

async function renderer() {
  const { transformWithOxc } = await import('vite');
  const source = fs.readFileSync(path.join(__dirname, 'QuotaEstimate.jsx'), 'utf8')
    .replace(/^import .*;\r?\n/gm, '').replace('export default function', 'function');
  const transformed = await transformWithOxc(source, 'QuotaEstimate.jsx', { jsx: { runtime: 'classic' } });
  const context = vm.createContext({ React, Intl, Date });
  vm.runInContext(transformed.code + '\nthis.Panel=QuotaEstimate;', context);
  return estimate => new JSDOM(renderToStaticMarkup(React.createElement(context.Panel, { estimate }))).window.document;
}

test('collapsed card is one summary line with pending, point or conditional range', async () => {
  const render = await renderer();
  const pending = render({ status: 'pending', reason: 'official_quota_stale', sampleCount: 5,
    historicalSampleCount: 3, retainedObservedTokens: 150000 });
  assert.equal(pending.querySelector('summary').textContent, '预计总额度待校准');
  assert.equal(pending.querySelector('details').open, false);
  assert.equal(pending.querySelector('.quota-estimate').children.length, 1);
  assert.doesNotMatch(pending.querySelector('summary').textContent, /\$0|Token|当前范围/);
  assert.match(pending.querySelector('details div').textContent, /当前范围 5 个 · 历史 3 个/);
  const point = render({ status: 'calibrated', estimatedTotalUsd: { estimate: 10, lower: 7.5, upper: 15 } });
  assert.equal(point.querySelector('summary').textContent, '预计总额度≈ $10.00');
  const range = render({ status: 'calibrated', conditionalTotalUsd: { lower: 7.5, upper: 60, sampleCount: 3 } });
  assert.equal(range.querySelector('summary').textContent, '预计总额度≈ $7.50–$60.00');
  assert.match(range.body.textContent, /条件范围假设/);
  assert.match(range.body.textContent, /未涵盖 Ultrafast/);
  range.querySelector('details').open = true;
  assert.equal(range.querySelector('details').open, true);
  assert.match(range.querySelector('.quota-estimate-detail').textContent, /不是订阅余额或实际账单/);
});

test('unknown, stale and invalid numbers never become a fabricated zero; reduction stays qualified', async () => {
  const render = await renderer();
  for (const value of [null, 0, -1, NaN, Infinity]) {
    const doc = render({ status: 'calibrated', estimatedTotalUsd: { estimate: value } });
    assert.equal(doc.querySelector('summary').textContent, '预计总额度待校准');
  }
  const stale = render({ status: 'pending', estimatedTotalUsd: { estimate: 20 } });
  assert.equal(stale.querySelector('summary').textContent, '预计总额度待校准');
  const tiny = render({ status: 'calibrated', estimatedTotalUsd: { estimate: .00001 } });
  assert.equal(tiny.querySelector('summary').textContent, '预计总额度≈ <$0.01');
  const doc = render({ status: 'calibrated', estimatedTotalUsd: { estimate: 20 },
    capacityComparison: { status: 'comparable_capacity_decline_signal', comparableWindowCount: 2,
      changePercent: { estimate: -50, lower: -70, upper: -20 }, baselineTotalUsd: { estimate: 40 } } });
  assert.doesNotMatch(doc.querySelector('summary').textContent, /下降/);
  assert.match(doc.body.textContent, /可比容量下降线索/);
  assert.match(doc.body.textContent, /不能证明官方削减/);
});
