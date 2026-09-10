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

test('quota row has no expandable explanation and supports point, range and pending', async () => {
  const render = await renderer();
  for (const [estimate, text] of [
    [{status:'pending', sampleCount:3, apiEquivalent:{knownUsd:10}}, '预计总额度待校准'],
    [{status:'calibrated', estimatedTotalUsd:{estimate:10}}, '预计总额度≈ $10.00'],
    [{status:'calibrated', conditionalTotalUsd:{lower:7.5,upper:60}}, '预计总额度≈ $7.50–$60.00'],
  ]) {
    const doc=render(estimate);
    assert.equal(doc.querySelector('.quota-estimate').textContent,text);
    assert.equal(doc.querySelectorAll('details,summary,p,button').length,0);
  }
});

test('invalid and stale totals never display a fabricated zero', async () => {
  const render=await renderer();
  for(const value of [null,0,-1,NaN,Infinity]) {
    assert.equal(render({status:'calibrated',estimatedTotalUsd:{estimate:value}}).body.textContent,'预计总额度待校准');
  }
  assert.equal(render({status:'pending',estimatedTotalUsd:{estimate:20}}).body.textContent,'预计总额度待校准');
  assert.equal(render({status:'calibrated',estimatedTotalUsd:{estimate:.00001}}).body.textContent,'预计总额度≈ <$0.01');
});
