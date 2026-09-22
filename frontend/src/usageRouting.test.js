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

test("upstream declaration is the fallback display label without a fingerprint candidate", () => {
  const result = usageModelRouting({
    requestedModel: "gpt-alias",
    routedModel: "gpt-alias",
    actualModel: "provider-native-model",
    modelEvidence: "actual",
  });
  assert.equal(result.displayModel, "provider-native-model");
  assert.equal(result.status, "mismatch");
  assert.equal(result.declaredModel, "provider-native-model");
  assert.equal(result.identityLabel, "上游声明 · 型号未确认");
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
  assert.equal(result.identityLabel, "仅路由 · 型号未确认");
});

test("unique fingerprint candidates drive the same model label used by filtering", () => {
  const result = usageModelRouting({
    requestedModel: "route-alias", actualModel: "upstream-alias", modelEvidence: "actual",
    modelIdentity: { status: "candidate", candidates: ["official-model"], referenceCount: 3 },
    modelFingerprint: { signature: "pfp_demo", protocols: ["openai_responses"], features: ["header:openai-version"] },
  });
  assert.equal(result.displayModel, "official-model");
  assert.equal(result.identityLabel, "指纹候选");
  assert.equal(result.declaredModel, "upstream-alias");
  assert.equal(result.expectedModel, "route-alias");
  assert.equal(result.referenceCount, 3);
  assert.equal(result.passiveFingerprint, "pfp_demo");
  assert.deepEqual(result.protocols, ["openai_responses"]);
});

test("official response records retain a distinct evidence label", () => {
  const result = usageModelRouting({
    model: "route", modelIdentity: { status: "reference", candidates: ["official-model"], referenceCount: 1 },
  });
  assert.equal(result.displayModel, "official-model");
  assert.equal(result.identityLabel, "官方响应");
});

test("ambiguous fingerprint candidates never silently select the first model", () => {
  const result = usageModelRouting({
    requestedModel: "route", actualModel: "declaration", modelEvidence: "actual",
    modelIdentity: { status: "candidate", candidates: ["first", "second"], referenceCount: 5 },
  });
  assert.equal(result.displayModel, "declaration");
  assert.equal(result.identityStatus, "ambiguous");
  assert.equal(result.identityCandidate, "");
  assert.deepEqual(result.identityCandidates, ["first", "second"]);
});

test("incomplete or unknown identities cannot promote a candidate", () => {
  for (const modelIdentity of [
    { status: "reference", candidates: [] },
    { status: "unknown", candidates: ["stale"] },
    { status: "candidate", candidates: [null, "unknown", ""] },
  ]) {
    const result = usageModelRouting({ model: "route", modelIdentity });
    assert.equal(result.displayModel, "route");
    assert.equal(result.identityStatus, "unknown");
  }
});

test("normalized usage rows retain the original route after candidate selection", () => {
  const result = usageModelRouting({
    model: "candidate", modelRoutingExpected: "original-route",
    modelIdentity: { status: "candidate", candidates: ["candidate"] },
  });
  assert.equal(result.displayModel, "candidate");
  assert.equal(result.expectedModel, "original-route");
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
