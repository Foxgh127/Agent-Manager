import "./QuotaEstimate.css";

const messages = {
  local_coverage_incomplete: "记录尚未完整，等待连续配对样本",
  official_quota_stale: "等待刷新官方额度",
  official_quota_unavailable: "等待同步官方额度",
  quota_window_or_plan_changed: "新周期或套餐，重新校准中",
  workload_changed: "模型或负载配置已变化，收集新样本",
  calibration_store_unavailable: "校准数据暂不可用",
};
const k = value => value != null && Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(Number(value) / 1000) + "K" : "未知";
const usd = value => value != null && Number.isFinite(Number(value)) ? Number(value) > 0 && Number(value) < .01 ? "<$0.01" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(Number(value)) : "未知";
const when = value => value ? new Date(typeof value === "number" ? value * 1000 : value).toLocaleString("zh-CN") : "尚无";
const positive = value => value != null && Number.isFinite(Number(value)) && Number(value) > 0;

export default function QuotaEstimate({ estimate }) {
  if (!estimate) return null;
  const total = estimate.status === "calibrated" && estimate.estimatedTotalUsd;
  const exact = total && positive(total.estimate);
  const range = estimate.status === "calibrated" && estimate.conditionalTotalUsd;
  const bounded = range && positive(range.lower) && positive(range.upper) && range.upper >= range.lower;
  const money = estimate.apiEquivalent;
  const comparison = estimate.capacityComparison;
  return <div className="quota-estimate" aria-label="额度估算">
    <details>
      <summary><span>预计总额度</span><strong>{exact ? `≈ ${usd(total.estimate)}` : bounded ? `≈ ${usd(range.lower)}–${usd(range.upper)}` : "待校准"}</strong></summary>
      <div className="quota-estimate-detail">
        <p>按本机主会话及网关记录，与官方额度百分比变化配对估算本周期总额；这是 API 美元等值，不是订阅余额或实际账单。</p>
        {exact ? <p>总额范围 {usd(total.lower)}–{usd(total.upper)}；{estimate.usdSampleCount} 个完整美元配对区间。</p> : bounded ? <p>条件总额范围 {usd(range.lower)}–{usd(range.upper)}，来自 {range.sampleCount} 个完整配对区间；未提供单一点值。</p> : <p>{messages[estimate.reason] || "收集连续配对样本中"}。至少需要 3 个完整区间、累计下降 6 个百分点；未知型号或未定价服务不能推算。</p>}
        {bounded && !exact && <p>条件范围假设记录型号即实际型号，服务仅为 Standard、Fast、Flex 或 Batch；未知请求上下文分别按 ≤272K 与 &gt;272K，未知缓存按输入内可行读写比例取边界。未涵盖 Ultrafast 等未定价档位，因此不是无条件保证。</p>}
        <p>当前范围 {estimate.sampleCount || 0} 个 · 历史 {estimate.historicalSampleCount || 0} 个；保留有效用量 {k(estimate.retainedObservedTokens)}，最近有效样本 {when(estimate.lastValidSampleAt)}。</p>
        {comparison?.status === "comparable_capacity_decline_signal" ? <p>可比容量下降线索：与 {comparison.comparableWindowCount} 个历史周期相比，变化 {comparison.changePercent?.estimate}%（范围 {comparison.changePercent?.lower}% 至 {comparison.changePercent?.upper}%）。历史总等值 {usd(comparison.baselineTotalUsd?.estimate)}。</p> : comparison?.status === "comparable_no_clear_decline" ? <p>已比较 {comparison.comparableWindowCount} 个历史周期，未发现明确容量下降；变化范围 {comparison.changePercent?.lower}% 至 {comparison.changePercent?.upper}%。历史总等值 {usd(comparison.baselineTotalUsd?.estimate)}。</p> : <p>尚无足够可比周期，不能判断容量是否下降。需同账号、套餐、模型配置及相近模型/上下文/服务/缓存/输出负载；当前与至少两个历史周期各需 5 个完整区间、下降 15 个百分点。</p>}
        <p>比较仅提示本机观察到的等值变化，不能证明官方削减；其他设备消耗、计费延迟及工具使用差异仍未识别。区间反映百分比取整与负载变化，不是统计置信区间。</p>
        {money && <p>已记录 API 等值 {usd(money.knownUsd)}{money.status === "partial" ? "（仅已知证据部分）" : ""}；未定价 {k(money.unpricedTokens)}。{money.legacyRowCount || estimate.legacyEvidenceRetained ? "旧记录已保留，缺少请求计价证据的部分不按短价冒算。" : ""}{!money.sourceHistoryComplete && "历史日志尚未完整收集。"}</p>}
        {money?.breakdown?.map(row => <p key={`${row.model}-${row.serviceTier}-${row.contextTier}`}>{row.model} · {row.serviceTier} · {row.contextTier === "long" ? "长上下文" : "短上下文"}：输入 {usd(row.uncachedInputUsd)} · 缓存读 {usd(row.cachedInputUsd)} · 缓存写 {usd(row.cacheWriteUsd)} · 输出 {usd(row.outputUsd)}</p>)}
        <p>请求输入超过 272K 时，整次输入与缓存费率 ×2、输出 ×1.5；缓存写为输入费率 ×1.25，Fast 为对应费率 ×2。缓存读写从输入总数内分拆，推理已包含在输出内。不含工具、区域处理及未识别模态附加费。<a href="https://developers.openai.com/api/docs/pricing" target="_blank" rel="noreferrer">官方价格</a>核实于 {money?.priceCheckedAt || "2026-09-10"}，后续可能变化。</p>
      </div>
    </details>
  </div>;
}
