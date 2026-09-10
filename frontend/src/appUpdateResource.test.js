import test from 'node:test';
import assert from 'node:assert/strict';
import { loadAppUpdate, subscribeAppUpdate } from './appUpdateResource.js';

test('startup check is shared and later page entries reuse its status', async () => {
  const calls = [];
  const api = async route => { calls.push(route); return { status: { configured: true, state: route.endsWith('/check') ? 'current' : 'not_checked' } }; };
  const observed = [];
  const first = loadAppUpdate(api, { force: true });
  assert.equal(loadAppUpdate(api, { force: true }), first);
  const unsubscribe = subscribeAppUpdate(api, state => observed.push(state.state));
  await first;
  unsubscribe();
  assert.equal((await loadAppUpdate(api)).state, 'current');
  assert.equal((await loadAppUpdate(api)).state, 'current');
  assert.deepEqual(calls, ['/api/app-update', '/api/app-update/check']);
  assert.deepEqual(observed, ['not_checked', 'current']);
  await loadAppUpdate(api, { force: true });
  assert.equal(calls.filter(route => route.endsWith('/check')).length, 2);
});

test('failed startup check remains a visible result instead of retrying on page entry', async () => {
  let checks = 0;
  const api = async route => {
    if (route.endsWith('/check')) { checks++; throw new Error('offline'); }
    return { status: { configured: true, state: 'not_checked' } };
  };
  await assert.rejects(loadAppUpdate(api, { force: true }), /offline/);
  assert.equal((await loadAppUpdate(api)).state, 'check_failed');
  assert.equal((await loadAppUpdate(api)).error, 'offline');
  assert.equal(checks, 1);
});
