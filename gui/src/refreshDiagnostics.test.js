import { test } from "node:test";
import assert from "node:assert/strict";
import { accountRefreshDiagnostics, diagnosticSummary } from "./refreshDiagnostics.js";
import { accountRefreshNotice } from "./modelRefresh.js";

test("network messages identify the operation instead of assuming quota failed", () => {
  const entries = accountRefreshDiagnostics({ refreshErrors: { models: "[SSL: UNEXPECTED_EOF_WHILE_READING]" } });
  assert.equal(entries[0].label, "模型目录");
  assert.match(diagnosticSummary(entries), /^模型目录：网络连接临时中断/);
  assert.doesNotMatch(diagnosticSummary(entries), /登录失效|额度接口/);
});
test("optional metadata warnings do not turn a successful refresh into a failure", () => {
  const account = { refreshState: "ready", refreshErrors: {}, refreshWarnings: { subscription: "timeout" } };
  assert.equal(accountRefreshDiagnostics(account)[0].severity, "warning");
  const notice = accountRefreshNotice(account, "A");
  assert.equal(notice.kind, "success");
  assert.match(notice.message, /订阅信息/);
  assert.doesNotMatch(notice.message, /部分刷新完成|刷新失败/);
});
test("all essential failures remain visible", () => {
  const account = { refreshState: "partial", refreshErrors: { usage: "HTTP 429", models: "timeout" } };
  const notice = accountRefreshNotice(account);
  assert.equal(notice.kind, "warning");
  assert.match(notice.message, /额度：HTTP 429/);
  assert.match(notice.message, /模型目录：连接超时/);
});
test("a successful refresh clears both kinds of old diagnostics", () => {
  assert.deepEqual(accountRefreshDiagnostics({ refreshErrors: {}, refreshWarnings: {} }), []);
});
