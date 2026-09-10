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
  coverage_resumed:"采样已恢复，历史区间已保留",
  workload_changed:"模型或推理设置已变化，收集新样本",
  local_counter_decreased:"计数基线已重建，历史区间已保留",
  calibration_store_unavailable:"校准数据暂不可用",
};
const k = value => value != null && Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN",{maximumFractionDigits:1}).format(Number(value)/1000) + "K" : "—";
const usd = value => value != null && Number.isFinite(Number(value)) ? new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",maximumFractionDigits:4}).format(Number(value)) : "未知";
const when = value => value ? new Date(typeof value === "number" ? value * 1000 : value).toLocaleString("zh-CN") : "尚无";

export default function QuotaEstimate({ estimate }) {
  if (!estimate) return null;
  const ready = estimate.status === "calibrated";
  const retained = estimate.retainedObservedTokens > 0;
  const observed = retained ? estimate.retainedObservedTokens : estimate.localCumulativeTokens;
  const money = estimate.apiEquivalent;
  const usdReady = ready && estimate.estimatedTotalUsd;
  return <div className="quota-estimate" aria-label="Token 用量推算">
    {ready ? <div className="quota-estimate-values">
      <span><small>{usdReady ? "估算已用 API 等值" : "估算已用 Token"}</small><strong>≈ {usdReady ? usd(estimate.estimatedUsedUsd?.estimate) : k(estimate.estimatedUsedTokens?.estimate)}</strong></span>
      <span><small>{usdReady ? "估算总量 API 等值" : "估算总量 Token"}</small><strong>≈ {usdReady ? usd(estimate.estimatedTotalUsd?.estimate) : k(estimate.estimatedTotalTokens?.estimate)}</strong></span>
    </div> : <p><span>Token 推算</span><small>{observed != null ? (retained ? "已保留有效采样 " : "本机采样 ") + k(observed) + " · " : ""}{messages[estimate.reason] || "等待连续配对样本"}</small></p>}
    <p className="quota-estimate-usd"><span>已记录 API 等值 {usd(money?.knownUsd)}</span><small>{money?.status === "partial" ? "仅已知价格部分" : money?.status === "available" ? "USD · 标准短上下文参考价" : "等待可定价的模型与用量明细"}</small></p>
    <details>
      <summary>{ready ? "按近期负载校准" : "校准进度"} · 当前范围 {estimate.sampleCount || 0} 个 · 历史 {estimate.historicalSampleCount || 0} 个</summary>
      <div>
        <p>当前范围限定同账号、套餐、额度周期及模型配置。累计保留有效区间用量 {k(estimate.retainedObservedTokens)}；最近有效样本 {when(estimate.lastValidSampleAt)}。</p>
        {ready && <p>总量范围 {k(estimate.estimatedTotalTokens?.lower)}–{k(estimate.estimatedTotalTokens?.upper)}；估算剩余 {k(estimate.estimatedRemainingTokens?.estimate)}。</p>}
        {usdReady && <p>美元等值总量范围 {usd(estimate.estimatedTotalUsd.lower)}–{usd(estimate.estimatedTotalUsd.upper)}；估算剩余 {usd(estimate.estimatedRemainingUsd?.estimate)}（{estimate.usdSampleCount} 个美元配对区间）。</p>}
        {!ready && <p>至少需要 3 个完整区间、累计下降 6 个百分点，才显示估算值。</p>}
        {ready && !usdReady && <p>美元额度推算等待至少 3 个可定价的完整配对区间；已记录美元与额度推算的数据范围不同。</p>}
        {money && <p>美元记录范围 {money.firstDate || "未知"}–{money.lastDate || "未知"}；记录时间 {when(money.observedAt)}。{!money.sourceHistoryComplete && "历史日志尚未完整收集。"}未定价 {k(money.unpricedTokens)}{money.unknownModels?.length ? `（${money.unknownModels.join("、")}）` : ""}。</p>}
        {money?.breakdown?.map(row => <p key={row.model}>{row.model}：输入 {usd(row.uncachedInputUsd)} · 缓存输入 {usd(row.cachedInputUsd)} · 输出 {usd(row.outputUsd)}</p>)}
        <p>美元按实际记录型号的标准短上下文 API 价折算，不是订阅余额或实际账单；不含长上下文、Fast、缓存写入和工具附加费。价目核实于 {money?.priceCheckedAt || "2026-09-10"}，价格可能变化。</p>
        <p>按本机主会话和网关记录推算等效 Token。模型、缓存、Fast、其他设备及未覆盖的原生子代理会影响比例；这不是官方固定 Token 上限。</p>
      </div>
    </details>
  </div>;
}
