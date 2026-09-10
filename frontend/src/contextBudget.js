// Codex clamps the requested total to the catalog ceiling, then reserves headroom.
export function contextBudget({ requested, defaultWindow, nativeMax, referenceMax, inputReferenceMax, effectivePercent }) {
  const positive = value => Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : 0;
  const configured = positive(requested);
  const total = configured || positive(defaultWindow);
  const reference = positive(inputReferenceMax) || positive(referenceMax);
  const ceiling = Math.max(positive(nativeMax), configured && reference ? Math.min(configured, reference) : 0);
  const resolved = ceiling ? Math.min(total, ceiling) : total;
  const percent = positive(effectivePercent);
  return {
    total: resolved,
    usable: resolved && percent && percent <= 100 ? Math.floor(resolved * percent / 100) : null,
    reservedPercent: percent && percent <= 100 ? 100 - percent : null,
    capped: Boolean(total && ceiling && total > ceiling),
  };
}
