import "./QuotaEstimate.css";

const messages = {
  local_coverage_incomplete:"记录正在追赶，待连续样本完整后校准",
  insufficient_intervals:"收集连续样本中",
  awaiting_paired_observations:"收集连续样本中",
  official_quota_stale:"等待刷新官方额度",
  official_quota_unavailable:"等待同步官方额度",
  observation_time_mismatch:"等待下一次同步样本",
  quota_regained:"额度已恢复，重新校准中",
  quota_window_or_plan_changed:"新周期或套餐，重新校准中",
  local_counter_generation_changed:"计数范围已变化，重新校准中",
  calibration_store_unavailable:"校准数据暂不可用",
};
const k = value => Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN",{maximumFractionDigits:1}).format(Number(value)/1000) + "K" : "—";

export default function QuotaEstimate({ estimate }) {
  if (!estimate) return null;
  const ready = estimate.status === "calibrated";
  const observed = estimate.localObservedTokens ?? estimate.localCumulativeTokens;
  return <div className="quota-estimate" aria-label="Token 用量推算">
    {ready ? <div className="quota-estimate-values">
      <span><small>估算已用</small><strong>≈ {k(estimate.estimatedUsedTokens?.estimate)}</strong></span>
      <span><small>估算总量</small><strong>≈ {k(estimate.estimatedTotalTokens?.estimate)}</strong></span>
    </div> : <p><span>Token 推算</span><small>{observed != null ? "本机采样 " + k(observed) + " · " : ""}{messages[estimate.reason] || "等待连续配对样本"}</small></p>}
    <details>
      <summary>{ready ? "按近期负载校准" : "校准进度"} · {estimate.sampleCount || 0} 个样本</summary>
      <div>
        {ready && <p>总量范围 {k(estimate.estimatedTotalTokens?.lower)}–{k(estimate.estimatedTotalTokens?.upper)}；估算剩余 {k(estimate.estimatedRemainingTokens?.estimate)}。</p>}
        {!ready && <p>至少需要 3 个完整区间、累计下降 6 个百分点，才显示估算值。</p>}
        <p>按本机主会话和网关记录推算等效 Token。模型、缓存、Fast、其他设备及未覆盖的原生子代理会影响比例；这不是官方固定 Token 上限。</p>
      </div>
    </details>
  </div>;
}
