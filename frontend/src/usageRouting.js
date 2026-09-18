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
 * The configured/routed model remains the primary label.  Actual response
 * metadata is surfaced only when it contradicts that route, so consistent
 * requests keep the compact table layout users already know.
 */
export function usageModelRouting(item = {}) {
  const requestedModel = usableModel(item.requestedModel);
  const routedModel = usableModel(item.routedModel);
  const fallbackModel = usableModel(item.model || item.modelName);
  const expectedModel = routedModel || requestedModel || fallbackModel;
  const displayModel = expectedModel || "未确定模型";
  const actualModel = item.modelEvidence === "actual" ? usableModel(item.actualModel) : "";
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
