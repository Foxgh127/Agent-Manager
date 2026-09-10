import assert from "node:assert/strict";
import test from "node:test";
import { usageSourceKey, usageSourceLabels, usageBackfillState } from "./usageViewModel.js";

test("identical labels and IDs remain distinct across account and provider", () => {
  const labels = usageSourceLabels([{ id: "same", label: "My account" }], [{ id: "same", name: "My account" }]);
  assert.notEqual(labels.get("account:same"), labels.get("provider:same"));
  const rows = [{ accountId: "same", account: "My account" }, { providerId: "same", account: "My account" }];
  assert.equal(rows.filter(item => usageSourceKey(item, "accounts") === "account:same").length, 1);
  assert.equal(usageSourceKey({ role: "mainAgent" }, "codex"), "role:mainAgent");
  assert.equal(usageSourceKey({}, "accounts"), "unknown:未归因账号");
});

test("pending files alone never claim an active history worker", () => {
  const state = status => usageBackfillState({backfill:{pendingFiles:12,status,workerRunning:false}});
  assert.match(state('blocked').text,/暂未完成读取.*手动刷新/);
  assert.equal(state('blocked').pollDelayMs,0);
  assert.match(state('pending').text,/等待补全/);
  assert.ok(state('retry').maxUnchangedPolls > 0);
  assert.match(state('paused').text,/已暂停/);
  assert.equal(state('paused').pollDelayMs,0);
  assert.match(usageBackfillState({backfill:{pendingFiles:12,workerRunning:true,status:'running'}}).text,/正在后台补全/);
  assert.equal(usageBackfillState({backfill:{pendingFiles:0}}).text,'');
});
