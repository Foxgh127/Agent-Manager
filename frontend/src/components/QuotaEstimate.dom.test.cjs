const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

test('quota card separates retained history, unknown dollars and priced projections', async () => {
  const { transformWithOxc } = await import('vite');
  const source = fs.readFileSync(path.join(__dirname, 'QuotaEstimate.jsx'), 'utf8')
    .replace(/^import .*;\r?\n/gm, '').replace('export default function', 'function');
  const transformed = await transformWithOxc(source, 'QuotaEstimate.jsx', { jsx: { runtime: 'classic' } });
  const context = vm.createContext({ React, Intl, Date });
  vm.runInContext(transformed.code + '\nthis.Panel=QuotaEstimate;', context);
  const render = estimate => renderToStaticMarkup(React.createElement(context.Panel, { estimate }));
  const pending = render({ status: 'pending', reason: 'official_quota_stale', sampleCount: 5,
    historicalSampleCount: 3, retainedObservedTokens: 150000, localCumulativeTokens: 1150000 });
  assert.match(pending, /当前范围 5 个 · 历史 3 个/);
  assert.match(pending, /已记录 API 等值 未知/);
  assert.doesNotMatch(pending, /\$0/);
  const ready = render({ status: 'calibrated', sampleCount: 3,
    estimatedTotalUsd: { estimate: 10, lower: 7.5, upper: 15 },
    estimatedUsedUsd: { estimate: 1.9 }, estimatedRemainingUsd: { estimate: 8.1 }, usdSampleCount: 3,
    apiEquivalent: { status: 'partial', knownUsd: 7.8, unknownModels: ['unknown-model'],
      breakdown: [{ model: 'gpt-6-astra', uncachedInputUsd: 2, cachedInputUsd: .8, outputUsd: 5 }] } });
  assert.match(ready, /估算总量 API 等值/);
  assert.match(ready, /\$10\.00/);
  assert.match(ready, /仅已知价格部分/);
  assert.match(ready, /缓存输入 \$0\.80/);
  assert.match(ready, /不是订阅余额或实际账单/);
});
