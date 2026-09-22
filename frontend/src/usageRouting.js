const UNKNOWN_MODELS = new Set(["", "unknown", "未确定模型"]);

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function usableModel(value) {
  const model = text(value);
  return UNKNOWN_MODELS.has(model.toLowerCase()) ? "" : model;
}

/**
 * Resolve the model evidence shown by the usage view.
 *
 * A unique fingerprint candidate is the display label when available.
 * Response model metadata is an upstream declaration, never proof of the
 * model's weights. The configured route remains separate for comparison.
 */
export function usageModelRouting(item = {}) {
  const requestedModel = usableModel(item.requestedModel);
  const routedModel = usableModel(item.routedModel);
  const fallbackModel = usableModel(item.model || item.modelName);
  const expectedModel = routedModel || requestedModel || usableModel(item.modelRoutingExpected) || fallbackModel;
  const actualModel = item.modelEvidence === "actual" ? usableModel(item.actualModel) : "";
  const identity = item.modelIdentity && typeof item.modelIdentity === "object" ? item.modelIdentity : {};
  const identityCandidates = [...new Set((Array.isArray(identity.candidates) ? identity.candidates : [])
    .map(usableModel).filter(Boolean))].slice(0, 8);
  const rawIdentityStatus = text(identity.status);
  const identityStatus = identityCandidates.length > 1 ? "ambiguous"
    : identityCandidates.length === 1 && ["candidate", "reference"].includes(rawIdentityStatus)
      ? rawIdentityStatus : "unknown";
  const identityCandidate = ["candidate", "reference"].includes(identityStatus) ? identityCandidates[0] : "";
  const displayModel = identityCandidate || actualModel || expectedModel || "未确定模型";
  const identityLabel = identityStatus === "reference" ? "官方响应"
    : identityStatus === "candidate" ? "指纹候选"
      : actualModel ? "上游声明 · 型号未确认" : "仅路由 · 型号未确认";
  const referenceCount = Number(identity.referenceCount);
  const modelFingerprint = item.modelFingerprint && typeof item.modelFingerprint === "object" ? item.modelFingerprint : {};
  const fingerprintTags = values => [...new Set((Array.isArray(values) ? values : [])
    .map(text).filter(Boolean))].slice(0, 24);
  let status = text(item.modelRoutingStatus).toLowerCase();
  if (actualModel && expectedModel) {
    // Recompute when evidence is present so a stale aggregate status cannot
    // hide a mismatch after a route or alias changes.
    status = actualModel.toLowerCase() === expectedModel.toLowerCase() ? "consistent" : "mismatch";
  } else if (!["consistent", "mismatch", "unknown"].includes(status)) {
    status = "unknown";
  }
  return {
    requestedModel,
    routedModel,
    expectedModel,
    displayModel,
    actualModel,
    declaredModel: actualModel,
    identityStatus,
    identityLabel,
    identityCandidate,
    identityCandidates,
    referenceCount: Number.isFinite(referenceCount) && referenceCount > 0 ? Math.floor(referenceCount) : 0,
    passiveFingerprint: text(modelFingerprint.signature),
    protocols: fingerprintTags(modelFingerprint.protocols),
    fingerprintFeatures: fingerprintTags(modelFingerprint.features),
    status,
    mismatch: status === "mismatch",
    fingerprint: text(item.systemFingerprint),
    fingerprintEvidence: text(item.fingerprintEvidence),
  };
}

export function usageRoutingSummary(records = []) {
  const summary = {
    observedRequests: 0,
    consistentRequests: 0,
    mismatchRequests: 0,
    unknownRequests: 0,
    fingerprintedRequests: 0,
  };
  const fingerprints = new Map();
  for (const item of records) {
    if (!item || typeof item !== "object") continue;
    const count = Math.max(0, Number(item.requestCount ?? item.requests ?? 1) || 0);
    if (!count) continue;
    const routing = usageModelRouting(item);
    summary.observedRequests += count;
    if (routing.status === "consistent") summary.consistentRequests += count;
    else if (routing.status === "mismatch") summary.mismatchRequests += count;
    else summary.unknownRequests += count;
    if (routing.fingerprint) {
      summary.fingerprintedRequests += count;
      const key = `${text(item.source)}|${text(item.sourceRecordId)}|${routing.expectedModel}`;
      if (!fingerprints.has(key)) fingerprints.set(key, new Set());
      fingerprints.get(key).add(routing.fingerprint);
    }
  }
  summary.fingerprintChanges = [...fingerprints.values()]
    .reduce((total, values) => total + Math.max(0, values.size - 1), 0);
  summary.method = "passive_response_metadata";
  return summary;
}
