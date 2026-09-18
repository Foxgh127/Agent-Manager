import assert from "node:assert/strict";
import test from "node:test";
import { usageModelRouting, usageRoutingSummary } from "./usageRouting.js";

test("consistent response model stays compact", () => {
  const result = usageModelRouting({
    requestedModel: "codex-default",
    routedModel: "gpt-6-astra",
    actualModel: "gpt-6-astra",
    modelEvidence: "actual",
  });
  assert.equal(result.displayModel, "gpt-6-astra");
  assert.equal(result.status, "consistent");
  assert.equal(result.mismatch, false);
});

test("mismatch exposes actual model and fingerprint evidence", () => {
  const result = usageModelRouting({
    requestedModel: "gpt-6-astra",
    routedModel: "gpt-6-astra",
    actualModel: "gpt-5.6-luna",
    modelEvidence: "actual",
    systemFingerprint: "fp_demo",
    fingerprintEvidence: "response_body",
  });
  assert.equal(result.status, "mismatch");
  assert.equal(result.actualModel, "gpt-5.6-luna");
  assert.equal(result.fingerprint, "fp_demo");
});

test("legacy rows stay unknown instead of producing a false warning", () => {
  const result = usageModelRouting({ requestedModel: "gpt-6-astra", actualModel: "gpt-5.6-luna" });
  assert.equal(result.status, "unknown");
  assert.equal(result.mismatch, false);
});

test("summary weights route findings by request count", () => {
  const result = usageRoutingSummary([
    { requestedModel: "a", routedModel: "a", actualModel: "a", modelEvidence: "actual", requestCount: 2 },
    { requestedModel: "a", routedModel: "a", actualModel: "b", modelEvidence: "actual", requestCount: 3, systemFingerprint: "fp1" },
  ]);
  assert.equal(result.observedRequests, 5);
  assert.equal(result.consistentRequests, 2);
  assert.equal(result.mismatchRequests, 3);
  assert.equal(result.unknownRequests, 0);
  assert.equal(result.fingerprintChanges, 0);
});
