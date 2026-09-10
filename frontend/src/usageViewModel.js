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

export function usageBackfillState(coverage) {
  const backfill = coverage?.backfill || {};
  const pending = Math.max(0, Number(backfill.pendingFiles) || 0);
  const running = backfill.workerRunning === true;
  const status = backfill.status || "pending";
  const state = { text: "", pollDelayMs: 0, maxUnchangedPolls: 0,
    progressKey: `${pending}:${backfill.scannedBytes || 0}:${backfill.remainingBytes || 0}:${status}` };
  if (!pending) return state;
  if (running) return { ...state, text: `正在后台补全历史记录 · 剩余 ${pending} 个文件`, pollDelayMs: 5000, maxUnchangedPolls: 12 };
  if (status === "paused") return { ...state, text: `历史补全已暂停 · 剩余 ${pending} 个文件` };
  if (status === "blocked") return { ...state, text: `部分历史暂未完成读取 · 剩余 ${pending} 个文件，可手动刷新重试` };
  return { ...state, text: `等待补全历史记录 · 剩余 ${pending} 个文件，可手动刷新重试`, pollDelayMs: 10000, maxUnchangedPolls: 6 };
}
