import assert from "node:assert/strict";
import test from "node:test";
import { usageSourceKey, usageSourceLabels } from "./usageViewModel.js";

test("identical labels and IDs remain distinct across account and provider", () => {
  const labels = usageSourceLabels([{ id: "same", label: "My account" }], [{ id: "same", name: "My account" }]);
  assert.notEqual(labels.get("account:same"), labels.get("provider:same"));
  const rows = [{ accountId: "same", account: "My account" }, { providerId: "same", account: "My account" }];
  assert.equal(rows.filter(item => usageSourceKey(item, "accounts") === "account:same").length, 1);
  assert.equal(usageSourceKey({ role: "mainAgent" }, "codex"), "role:mainAgent");
  assert.equal(usageSourceKey({}, "accounts"), "unknown:未归因账号");
});
