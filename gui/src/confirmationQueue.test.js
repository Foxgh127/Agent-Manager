import assert from "node:assert/strict";
import test from "node:test";
import { createConfirmationQueue } from "./confirmationQueue.js";

test("simultaneous confirmations each receive their own answer", async () => {
  let shown;
  const queue = createConfirmationQueue(value => { shown = value; });
  const first = queue.ask({ title: "First" });
  const firstId = shown.id;
  const second = queue.ask({ title: "Second" });
  assert.equal(shown.title, "First");
  queue.resolve(false, firstId);
  assert.equal(await first, false);
  assert.equal(shown.title, "Second");
  queue.resolve(true, firstId); // A stale double click cannot approve the next operation.
  assert.equal(shown.title, "Second");
  queue.resolve(true, shown.id);
  assert.equal(await second, true);
  assert.equal(shown, null);
});

test("unmount cancels queued choices without leaving pending work", async () => {
  const queue = createConfirmationQueue(() => {});
  const first = queue.ask({});
  const second = queue.ask({ choiceMode: true });
  queue.cancelAll();
  assert.deepEqual(await Promise.all([first, second]), [false, "cancel"]);
});
