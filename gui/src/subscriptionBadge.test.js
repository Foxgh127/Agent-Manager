import { test } from "node:test";
import assert from "node:assert/strict";
import { subscriptionBadge } from "./subscriptionBadge.js";

test("maps Pro multipliers only when they are explicit Pro aliases", () => {
  assert.deepEqual(subscriptionBadge(["Pro 5x"]), {
    tier: "silver",
    label: "Pro 5x",
  });
  assert.deepEqual(subscriptionBadge(["pro_max"]), {
    tier: "gold",
    label: "Pro 20x",
  });
  assert.deepEqual(subscriptionBadge(["prolite"]), {
    tier: "silver",
    label: "Pro 5x",
  });
  assert.deepEqual(subscriptionBadge(["chatgpt-pro-20x"]), {
    tier: "gold",
    label: "Pro 20x",
  });
  assert.deepEqual(subscriptionBadge(["Team 20x capacity"]), {
    tier: "neutral",
    label: "Team 20x capacity",
  });
});

test("prefers a recognized displayed label over stale usage data", () => {
  assert.deepEqual(subscriptionBadge(["Plus", "plus", "pro20x"]), {
    tier: "bronze",
    label: "Plus",
  });
  assert.deepEqual(subscriptionBadge(["Pro 5x", "pro5x", "free"]), {
    tier: "silver",
    label: "Pro 5x",
  });
});

test("recognizes organization plans and preserves unknown display text", () => {
  assert.deepEqual(subscriptionBadge(["ChatGPT Business"]), {
    tier: "business",
    label: "Business",
  });
  assert.deepEqual(subscriptionBadge(["Education"]), {
    tier: "edu",
    label: "Edu",
  });
  assert.deepEqual(subscriptionBadge(["Founders Preview", "mystery"]), {
    tier: "neutral",
    label: "Founders Preview",
  });
});

test("free account evidence remains authoritative", () => {
  assert.deepEqual(subscriptionBadge(["Pro 20x"], true), {
    tier: "free",
    label: "Free",
  });
});

test("generic Pro maps to twenty-x and explicit five-x remains distinct", () => {
  assert.deepEqual(subscriptionBadge(["Pro", "pro"]), { tier: "gold", label: "Pro 20x" });
  assert.deepEqual(subscriptionBadge(["Pro", "pro", "pro20x"]), { tier: "gold", label: "Pro 20x" });
  assert.deepEqual(subscriptionBadge(["Pro", "pro5x"]), { tier: "silver", label: "Pro 5x" });
});

test("supports explicit Codex aliases and product cadence without guessing unknown IDs", () => {
  assert.deepEqual(subscriptionBadge(["codex-pro-20x"]), { tier: "gold", label: "Pro 20x" });
  assert.deepEqual(subscriptionBadge(["chatgpt_pro_20x_monthly"]), { tier: "gold", label: "Pro 20x" });
  assert.deepEqual(subscriptionBadge(["codex-pro-5x"]), { tier: "silver", label: "Pro 5x" });
  assert.deepEqual(subscriptionBadge(["prod_unknown_20x"]), { tier: "neutral", label: "prod_unknown_20x" });
});

test("explicit current variants retain priority over stale lower-ranked variants", () => {
  assert.deepEqual(subscriptionBadge(["Pro 20x", "pro5x"]), { tier: "gold", label: "Pro 20x" });
  assert.deepEqual(subscriptionBadge(["Pro 5x", "pro20x"]), { tier: "silver", label: "Pro 5x" });
});
