/** Apply the acknowledged placement only, preserving unrelated card data/refs. */
export function applyDashboardMoveResult(data, result) {
  if (!data || !result) return data;
  const settings = { ...data.settings };
  const groups = result.groupAssignments || {};
  for (const collection of ["accounts", "providers", "relayAccounts"]) {
    const changes = groups[collection];
    if (changes && Object.keys(changes).length) {
      settings[collection] = (settings[collection] || []).map(item =>
        Object.hasOwn(changes, item.id) ? { ...item, groupId: changes[item.id] } : item);
    }
  }
  let modelSources = data.modelSources;
  if (Object.keys(groups.accounts || {}).length || Object.keys(groups.providers || {}).length) {
    modelSources = (modelSources || []).map(source => {
      const changes = groups[source.kind === "account" ? "accounts" : "providers"] || {};
      return Object.hasOwn(changes, source.recordId) ? { ...source, groupId: changes[source.recordId] } : source;
    });
  }
  if (Array.isArray(result.dashboardOrder)) settings.dashboardOrder = result.dashboardOrder;
  let web2apiStatus = data.web2apiStatus;
  if (result.dropTarget === "apiPool" && result.pool) {
    settings.web2api = { ...settings.web2api, ...result.pool };
    for (const [collection, field] of [["accounts", "accountIds"], ["providers", "providerIds"]]) {
      const members = new Set(result.pool[field] || []);
      settings[collection] = (settings[collection] || []).map(item =>
        Boolean(item.proxyEnabled) === members.has(item.id) ? item : { ...item, proxyEnabled: members.has(item.id) });
    }
    web2apiStatus = { ...web2apiStatus, memberCount: (result.pool.accountIds?.length || 0) + (result.pool.providerIds?.length || 0) };
  }
  return { ...data, settings, modelSources, web2apiStatus };
}
