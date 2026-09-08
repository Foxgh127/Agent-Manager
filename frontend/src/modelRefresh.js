import { accountRefreshDiagnostics, diagnosticSummary } from "./refreshDiagnostics.js";

export function accountRefreshNotice(account, name = "账号") {
  if (account?.refreshSkipped === "backoff") {
    return { kind: "warning", message: `${name} 正在等待远端限流恢复，保留已有模型与额度；请在 ${account.nextRefreshAt || "稍后"} 重试` };
  }
  const entries = accountRefreshDiagnostics(account);
  const errors = entries.filter(item => item.severity === "error");
  if (account?.refreshState === "error" || account?.refreshState === "partial" || errors.length) {
    return { kind: account?.refreshState === "error" ? "error" : "warning", message: `${name} ${account?.refreshState === "error" ? "刷新失败" : "部分刷新完成"}：${diagnosticSummary(errors) || "部分数据暂不可用，已保留上次结果"}` };
  }
  const warnings = entries.filter(item => item.severity === "warning");
  return { kind: "success", message: `${name} 的模型与额度已刷新${warnings.length ? `；${warnings.map(item => item.label).join("、")}暂未更新，详情见卡片` : ""}` };
}

export function batchRefreshNotice(results) {
  let failures = 0;
  let partial = 0;
  let skipped = 0;
  for (const result of results) {
    if (result.status === "rejected") { failures++; continue; }
    const value = result.value || {};
    if (value.relayRefresh) {
      if (value.result?.requiresLogin || value.result?.warnings?.length || value.result?.requiresReapply) partial++;
      continue;
    }
    if (value.account?.refreshState === "error") failures++;
    else if (value.account?.refreshState === "partial") partial++;
    if (value.account?.refreshSkipped) skipped++;
    failures += Number(value.result?.error || 0);
    partial += Number(value.result?.partial || 0);
    skipped += Number(value.result?.skipped || 0);
    if (value.discovery?.status === "stale" || value.balanceWarning) partial++;
  }
  return failures || partial || skipped
    ? { kind: "warning", message: `刷新结束：${failures} 项失败，${partial} 项部分更新，${skipped} 项沿用近期结果；详情见账号卡片` }
    : { kind: "success", message: "全部账号、模型及中转站 Key、分组与额度已刷新" };
}

export function reconcileModelSelection(selected, previousSources, nextSources) {
  const next = new Set(selected);
  const previous = new Map(previousSources.map(source => [source.id, source]));
  for (const source of nextSources) {
    const before = previous.get(source.id)?.models || [];
    const followAll = before.length > 0 && before.every(model => selected.has(model.key));
    const available = new Set(source.models.map(model => model.key));
    for (const model of before) if (!available.has(model.key)) next.delete(model.key);
    if (followAll) for (const key of available) next.add(key);
  }
  const remainingSources = new Set(nextSources.map(source => source.id));
  for (const source of previousSources) {
    if (!remainingSources.has(source.id)) for (const model of source.models) next.delete(model.key);
  }
  return next;
}

export async function runBoundedRefresh(jobs, concurrency = 3) {
  const results = new Array(jobs.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(concurrency, jobs.length) }, async () => {
    while (next < jobs.length) {
      const index = next++;
      try { results[index] = { status: "fulfilled", value: await jobs[index]() }; }
      catch (reason) { results[index] = { status: "rejected", reason }; }
    }
  }));
  return results;
}
