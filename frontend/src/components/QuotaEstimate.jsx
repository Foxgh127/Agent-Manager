import "./QuotaEstimate.css";

const usd = value => value != null && Number.isFinite(Number(value)) ? Number(value) > 0 && Number(value) < .01 ? "<$0.01" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(Number(value)) : "未知";
const positive = value => value != null && Number.isFinite(Number(value)) && Number(value) > 0;

export default function QuotaEstimate({ estimate }) {
  if (!estimate) return null;
  const total = estimate.status === "calibrated" && estimate.estimatedTotalUsd;
  const exact = total && positive(total.estimate);
  const range = estimate.status === "calibrated" && estimate.conditionalTotalUsd;
  const bounded = range && positive(range.lower) && positive(range.upper) && range.upper >= range.lower;
  const value = exact ? `≈ ${usd(total.estimate)}` : bounded ? `≈ ${usd(range.lower)}–${usd(range.upper)}` : "待校准";
  return <div className="quota-estimate" aria-label="预计总额度">
    <span>预计总额度</span><strong title={value}>{value}</strong>
  </div>;
}
