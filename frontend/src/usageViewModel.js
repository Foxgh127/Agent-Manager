export function usageSourceKey(item, source) {
  if (source === "codex") return `role:${item.role || item.agentRole || "unclassified"}`;
  if (item.accountId) return `account:${item.accountId}`;
  if (item.providerId) return `provider:${item.providerId}`;
  return `unknown:${item.account || item.accountName || "未归因账号"}`;
}

export function usageSourceLabels(accounts = [], providers = []) {
  const entries = [
    ...accounts.map(item => [`account:${item.id}`, item.label || item.email || item.id]),
    ...providers.map(item => [`provider:${item.id}`, item.name || item.label || item.id]),
  ];
  const counts = new Map();
  for (const [, label] of entries) counts.set(label, (counts.get(label) || 0) + 1);
  return new Map(entries.map(([id, label]) => [id, counts.get(label) > 1 ? `${label} · ${id}` : label]));
}
