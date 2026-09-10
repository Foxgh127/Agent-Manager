import test from "node:test";
import assert from "node:assert/strict";
import { contextBudget } from "./contextBudget.js";

test("a 1M setting observes both the model input ceiling and Codex headroom", () => {
  assert.deepEqual(contextBudget({ requested: 1000000, defaultWindow: 272000,
    nativeMax: 872000, referenceMax: 1050000, inputReferenceMax: 922000, effectivePercent: 95 }),
  { total: 922000, usable: 875900, reservedPercent: 5, capped: true });
});

test("model defaults and provider limits do not acquire the official 1M extension", () => {
  assert.equal(contextBudget({ requested: 0, defaultWindow: 272000,
    nativeMax: 872000, referenceMax: 1050000, effectivePercent: 95 }).usable, 258400);
  assert.deepEqual(contextBudget({ requested: 1000000, defaultWindow: 64000,
    nativeMax: 128000, referenceMax: 0, effectivePercent: 90 }),
  { total: 128000, usable: 115200, reservedPercent: 10, capped: true });
});

test("unknown reservation stays unknown and values beyond verified capacity are capped", () => {
  assert.equal(contextBudget({ requested: 1000000, referenceMax: 0 }).usable, null);
  assert.deepEqual(contextBudget({ requested: 2000000, nativeMax: 872000,
    referenceMax: 1050000, inputReferenceMax: 922000, effectivePercent: 95 }),
  { total: 922000, usable: 875900, reservedPercent: 5, capped: true });
});
