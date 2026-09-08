export function matchesAccount(source = {}, record = {}, { query = "", groupId = "all" } = {}) {
  if (groupId !== "all" && String(record.groupId || source.groupId) !== groupId) return false;
  const words = query.trim().toLocaleLowerCase("zh-CN").split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  // Search public labels only. API keys, OAuth tokens and imported raw JSON
  // must never become searchable or appear in a client-side search index.
  const text = [source.name, source.subtitle, source.groupName, record.label,
    record.email, record.siteName, record.portalUrl, record.baseUrl,
    ...(source.models || []).map(model => model.id)].filter(Boolean).join(" ").toLocaleLowerCase("zh-CN");
  return words.every(word => text.includes(word));
}

export function visibleSourceIds(sourceRows, relayRows) {
  return [...new Set([...sourceRows.map(source => source.id), ...relayRows.map(row => row.source?.id)].filter(Boolean))];
}
