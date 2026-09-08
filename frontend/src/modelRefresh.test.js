import { test } from "node:test";
import assert from "node:assert/strict";
import { accountRefreshNotice, batchRefreshNotice, reconcileModelSelection, runBoundedRefresh } from "./modelRefresh.js";

const source = ids => ({ id: "account:test", models: ids.map(id => ({ key: `account:test::${id}` })) });
test("failed and deferred account refreshes never claim success", () => {
  assert.equal(accountRefreshNotice({ refreshState: "partial", refreshErrors: { models: "offline" } }).kind, "warning");
  assert.equal(accountRefreshNotice({ refreshState: "error" }).kind, "error");
  assert.equal(accountRefreshNotice({ refreshSkipped: "backoff" }).kind, "warning");
  assert.equal(accountRefreshNotice({ refreshState: "ready" }).kind, "success");
});
test("batch keeps provider failures and account partial states visible", () => {
  const notice = batchRefreshNotice([{ status: "rejected", reason: new Error("offline") },
    { status: "fulfilled", value: { result: { error: 2, partial: 1, skipped: 1 } } }]);
  assert.equal(notice.kind, "warning");
  assert.match(notice.message, /3 项失败/);
  assert.match(notice.message, /1 项部分更新/);
});
test("batch relay login and incomplete catalog warnings do not claim full success", () => {
  const notice = batchRefreshNotice([
    {status:"fulfilled",value:{relayRefresh:true,result:{requiresLogin:true}}},
    {status:"fulfilled",value:{relayRefresh:true,result:{warnings:["分页不完整，保留本机 Key"]}}},
  ]);
  assert.equal(notice.kind,"warning");
  assert.match(notice.message,/2 项部分更新/);
});
test("new models follow a fully selected source while curated selections stay curated", () => {
  const previous = [source(["old", "kept"])];
  const next = [source(["kept", "gpt-6-astra"])];
  const all = reconcileModelSelection(new Set(previous[0].models.map(m => m.key)), previous, next);
  assert.deepEqual([...all].sort(), next[0].models.map(m => m.key).sort());
  const curated = reconcileModelSelection(new Set(["account:test::kept"]), previous, next);
  assert.deepEqual([...curated], ["account:test::kept"]);
});
test("batch refresh bounds concurrent jobs and drains failures", async () => {
  let active = 0, maxActive = 0, completed = 0;
  const results = await runBoundedRefresh(Array.from({ length: 12 }, (_, index) => async () => {
    active++; maxActive = Math.max(maxActive, active);
    await new Promise(resolve => setTimeout(resolve, 2));
    active--; completed++;
    if (index === 4) throw new Error("expected");
    return index;
  }), 3);
  assert.equal(maxActive, 3);
  assert.equal(completed, 12);
  assert.equal(results[4].status, "rejected");
  assert.equal(results[11].value, 11);
});
