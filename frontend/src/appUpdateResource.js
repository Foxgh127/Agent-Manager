// One shared update status per API client, surviving settings-page remounts.
const resources = new WeakMap();
function resource(api) {
  if (!resources.has(api)) resources.set(api, { status: null, read: null, check: null, listeners: new Set() });
  return resources.get(api);
}
export function rememberAppUpdate(api, status) {
  if (status) resource(api).status = status;
}
function publish(api, status) {
  if (!status) return status;
  const entry = resource(api);
  entry.status = status;
  for (const listener of entry.listeners) listener(status);
  return status;
}
export function subscribeAppUpdate(api, listener) {
  const entry = resource(api);
  entry.listeners.add(listener);
  if (entry.status) listener(entry.status);
  return () => entry.listeners.delete(listener);
}
function read(api) {
  const entry = resource(api);
  if (!entry.read) entry.read = api('/api/app-update').then(r => publish(api, r.status)).finally(() => { entry.read = null; });
  return entry.read;
}
export function loadAppUpdate(api, { force = false } = {}) {
  const entry = resource(api);
  if (!force) return entry.check || (entry.status ? Promise.resolve(entry.status) : read(api));
  if (!entry.check) entry.check = (async () => {
    const current = await read(api);
    if (!current?.configured || current.download?.state === 'downloading' || ['waiting_for_exit', 'installed'].includes(current.installation?.state)) return current;
    const result = await api('/api/app-update/check', { method: 'POST', body: '{}', timeoutMs: 60000 });
    return publish(api, result.status);
  })().catch(error => {
    publish(api, { ...(entry.status || {}), state: 'check_failed', error: error.message });
    throw error;
  }).finally(() => { entry.check = null; });
  return entry.check;
}
