export function quotaIsCurrent(quota, unavailable = false, now = Date.now()) {
  if (!quota || quota.remainingPercent == null || unavailable || quota.stale) return false;
  if (!Number.isFinite(Number(quota.remainingPercent))) return false;
  const reset = quota.resetAt ? Date.parse(quota.resetAt) : NaN;
  return !Number.isFinite(reset) || reset > now;
}
