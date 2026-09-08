const { test } = require("node:test");
const assert = require("node:assert/strict");
const geometry = import("./dragGeometry.js");
const rect = (x, y, width = 200, height = 200) => ({ left: x, top: y, right: x + width, bottom: y + height, width, height });
const slots = [rect(0, 100), rect(220, 100), rect(440, 100), rect(0, 320), rect(220, 320), rect(440, 320)];

test("grid rows and columns resolve horizontal, vertical, diagonal and gap positions", async () => {
  const { gridSlotAt } = await geometry;
  for (const [point, expected] of [[{ x: 300, y: 150 }, 1], [{ x: 50, y: 400 }, 3], [{ x: 520, y: 400 }, 5], [{ x: 440, y: 310 }, 5]]) {
    assert.equal(gridSlotAt(slots, point), expected);
  }
});
test("slot hysteresis retains the preview when the pointer jitters around a boundary", async () => {
  const { gridSlotAt } = await geometry;
  assert.equal(gridSlotAt(slots, { x: 218, y: 150 }, 0), 0);
  assert.equal(gridSlotAt(slots, { x: 225, y: 150 }, 0), 1);
  assert.equal(gridSlotAt(slots, { x: 205, y: 150 }, 1), 1);
  assert.equal(gridSlotAt(slots, { x: 190, y: 150 }, 1), 0);
  assert.equal(gridSlotAt(slots, { x: 100, y: 315 }, 0), 0);
  assert.equal(gridSlotAt(slots, { x: 100, y: 330 }, 0), 3);
});
test("single column uses vertical position; incomplete final row has a stable last slot", async () => {
  const { gridSlotAt } = await geometry;
  assert.equal(gridSlotAt([rect(0, 100), rect(0, 320)], { x: 10, y: 490 }, 0), 1);
  assert.equal(gridSlotAt(slots.slice(0, 4), { x: 620, y: 440 }, 2), 3);
});
test("viewport scrolling shifts all slot bounds without changing the intended destination", async () => {
  const { gridSlotAt } = await geometry;
  const scrolled = slots.map(r => ({ ...r, top: r.top - 160, bottom: r.bottom - 160 }));
  assert.equal(gridSlotAt(scrolled, { x: 300, y: 240 }, 0), 4);
});
test("preview permutation and drop payload keep the original visible subset", async () => {
  const { moveCardToSlot, cardOrderPayload } = await geometry;
  const ids = ["a", "b", "c", "d"];
  const preview = moveCardToSlot(ids, "a", 2);
  assert.deepEqual(preview, ["b", "c", "a", "d"]);
  assert.deepEqual(ids, ["a", "b", "c", "d"]);
  assert.deepEqual(cardOrderPayload(ids, preview, "a"), { sourceId: "a", visibleIds: ids, beforeId: "d" });
  assert.deepEqual(cardOrderPayload(ids, moveCardToSlot(ids, "a", 3), "a"), { sourceId: "a", visibleIds: ids, afterId: "d" });
  assert.equal(cardOrderPayload(ids, ids, "a"), null);
});
test("target proximity docks the ghost but never commits until the pointer enters", async () => {
  const { cardDropTarget } = await geometry;
  const group = { node: {}, type: "group", rect: rect(100, 0, 90, 40) };
  let result = cardDropTarget([group], { x: 150, y: 70 });
  assert.equal(result.hit, null); assert.equal(result.near.node, group.node);
  result = cardDropTarget([group], { x: 150, y: 20 }); assert.equal(result.hit.node, group.node);
  assert.equal(cardDropTarget([group], { x: 194, y: 20 }, group.node).hit.node, group.node);
  assert.equal(cardDropTarget([group], { x: 198, y: 20 }, group.node).hit, null);
});
test("API pool is an explicit target and does not steal a smaller overlapping tab", async () => {
  const { cardDropTarget } = await geometry;
  const pool = { node: {}, type: "apiPool", rect: rect(0, 0, 800, 100) };
  const group = { node: {}, type: "group", rect: rect(100, 0, 90, 40) };
  assert.equal(cardDropTarget([pool, group], { x: 150, y: 20 }).hit.type, "group");
  assert.equal(cardDropTarget([pool, group], { x: 400, y: 70 }).hit.type, "apiPool");
});

test("pool docking starts at its bottom edge, never in the approach area", async () => {
  const { cardDropTarget } = await geometry;
  const pool = { node: {}, type: "apiPool", rect: rect(200, 100, 800, 100) };
  for (const y of [280, 240, 201, 200.01]) {
    assert.equal(cardDropTarget([pool], {x:600,y}).near, null);
  }
  assert.equal(cardDropTarget([pool], {x:600,y:200}).near.type,"apiPool");
  assert.equal(cardDropTarget([pool], {x:600,y:199}).hit.type,"apiPool");
  assert.equal(cardDropTarget([pool], {x:600,y:201},pool.node).near,null);
  assert.equal(cardDropTarget([pool], {x:199,y:150}).near,null);
});
test("docked thumbnail stays fully outside group/pool in wide and narrow viewports", async () => {
  const { dockCardPreview } = await geometry;
  for (const [target, viewport] of [[rect(100, 0, 80, 40), { width: 1000, height: 800 }], [rect(10, 0, 300, 80), { width: 320, height: 600 }]]) {
    const result = dockCardPreview(target, 400, 600, viewport), w = 400 * result.scale, h = 600 * result.scale;
    assert.ok(result.scale < .33);
    assert.ok(result.x >= target.right || result.x + w <= target.left || result.y >= target.bottom || result.y + h <= target.top);
  }
});
