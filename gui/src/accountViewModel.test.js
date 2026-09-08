import { test } from "node:test";
import assert from "node:assert/strict";
import { matchesAccount, visibleSourceIds } from "./accountViewModel.js";

const source = { id: "account:a", name: "Work", available: true, models: [{ id: "gpt-6-astra" }] };
const record = { groupId: "work", email: "demo@example.test", apiKey: "never-index-this-secret" };
test("account search combines words and excludes credentials", () => {
  assert.equal(matchesAccount(source, record, { query: "demo ASTRA", groupId: "work" }), true);
  assert.equal(matchesAccount(source, record, { query: "never-index-this-secret" }), false);
  assert.equal(matchesAccount(source, record, { query: "demo", groupId: "other" }), false);
});
test("selecting visible results includes relay providers once", () => {
  assert.deepEqual(visibleSourceIds([source], [{ source: { id: "provider:r" } }, { source }, {}]), ["account:a", "provider:r"]);
});
