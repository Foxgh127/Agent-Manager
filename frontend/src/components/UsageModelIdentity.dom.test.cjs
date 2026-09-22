const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { JSDOM } = require("jsdom");

async function renderer() {
  const [{ transformWithOxc }, { usageModelRouting }] = await Promise.all([
    import("vite"), import("../usageRouting.js"),
  ]);
  const source = fs.readFileSync(path.join(__dirname, "UsageModelIdentity.jsx"), "utf8")
    .replace(/^import .*;\r?\n/gm, "").replace("export default function", "function");
  const transformed = await transformWithOxc(source, "UsageModelIdentity.jsx", { jsx: { runtime: "classic" } });
  const context = vm.createContext({ React, usageModelRouting });
  vm.runInContext(transformed.code + "\nthis.Panel = UsageModelIdentity;", context);
  return item => new JSDOM(renderToStaticMarkup(React.createElement(context.Panel, { item }))).window.document;
}

test("candidate label distinguishes declaration and opens useful fingerprint evidence", async () => {
  const render = await renderer();
  const doc = render({
    requestedModel: "route", actualModel: "claimed-model", modelEvidence: "actual",
    systemFingerprint: "fp_reference",
    modelFingerprint: { signature: "pfp_structural", protocols: ["openai_responses"], features: ["header:openai-version"] },
    modelIdentity: { status: "candidate", candidates: ["candidate-model"], referenceCount: 4 },
  });
  assert.equal(doc.querySelector(".usage-model-name").textContent, "candidate-model");
  assert.equal(doc.querySelector(".usage-model-evidence").textContent, "指纹候选");
  const details = doc.querySelector("details");
  assert.equal(details.open, false);
  details.querySelector("summary").click();
  assert.equal(details.open, true);
  for (const value of ["claimed-model", "fp_reference", "pfp_structural", "OpenAI Responses", "header:openai-version", "官方参考数"]) {
    assert.ok(details.textContent.includes(value), value);
  }
  assert.equal(doc.querySelector("dl dd:last-child").textContent, "4");
  details.querySelector("summary").click();
  assert.equal(details.open, false);
  assert.ok(!doc.body.textContent.includes("100%"));
});

test("ambiguous identity exposes every candidate without replacing the declaration", async () => {
  const render = await renderer();
  const doc = render({
    actualModel: "claimed-model", modelEvidence: "actual",
    modelIdentity: { status: "ambiguous", candidates: ["model-a", "model-b"] },
  });
  assert.equal(doc.querySelector(".usage-model-name").textContent, "claimed-model");
  assert.equal(doc.querySelector(".usage-model-evidence").textContent, "上游声明 · 型号未确认");
  assert.ok(doc.querySelector("summary").textContent.includes("2 个候选"));
  doc.querySelector("summary").click();
  assert.equal(doc.querySelector("details").open, true);
  assert.deepEqual([...doc.querySelectorAll(".usage-model-candidates code")].map(node => node.textContent), ["model-a", "model-b"]);
  assert.ok(doc.querySelector(".usage-model-ambiguous").textContent.includes("无法唯一判断"));
});

test("official reference and route-only rows have separate honest labels", async () => {
  const render = await renderer();
  const reference = render({ modelIdentity: { status: "reference", candidates: ["official-model"] } });
  assert.equal(reference.querySelector(".usage-model-evidence").textContent, "官方响应");
  const route = render({ model: "route-only", actualModel: "unverified-legacy" });
  assert.equal(route.querySelector(".usage-model-name").textContent, "route-only");
  assert.equal(route.querySelector(".usage-model-evidence").textContent, "仅路由 · 型号未确认");
  assert.ok(!route.body.textContent.includes("unverified-legacy"));
  assert.equal(route.querySelectorAll("a").length, 2);
  for (const link of route.querySelectorAll("a")) {
    assert.equal(new URL(link.href).hostname, "github.com");
    assert.match(link.href, /\/blob\/[0-9a-f]{40}\//);
    assert.equal(link.rel, "noopener noreferrer");
  }
});
