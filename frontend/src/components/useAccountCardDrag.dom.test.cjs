// Real React lifecycle and jsdom event propagation, with deterministic geometry.
// Run npm ci in gui; CARD_DRAG_TEST_NODE_MODULES can override the locked test runtime.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const dependencies = process.env.CARD_DRAG_TEST_NODE_MODULES || path.resolve(__dirname, "../../node_modules");
const { JSDOM } = require(path.join(dependencies, "jsdom"));
const React = require(path.join(dependencies, "react"));
const { act } = React;

test("real React mount/ref replacement and actual card span structures support blank-space dragging", async () => {
  const dom = new JSDOM("<!doctype html><body><div id='root'></div></body>", { pretendToBeVisual: true });
  const { window } = dom;
  // jsdom does not implement the standards-mode viewport scrolling element.
  Object.defineProperty(window.document, "scrollingElement", { value: window.document.documentElement });
  const previous = { window: global.window, document: global.document, flag: global.IS_REACT_ACT_ENVIRONMENT };
  global.window = window; global.document = window.document; global.IS_REACT_ACT_ENVIRONMENT = true;
  let reduceMotion = true;
  window.matchMedia = () => ({ matches: reduceMotion });
  const animations = [];
  window.HTMLElement.prototype.animate = function (frames) {
    animations.push({ cardId: this.dataset.cardId, frames });
    return { finished: Promise.resolve(), cancel() {} };
  };
  const { createRoot } = require(path.join(dependencies, "react-dom/client"));
  const rect = (left, top, width, height) => ({ left, top, right: left + width, bottom: top + height, width, height, x: left, y: top });
  window.HTMLElement.prototype.getBoundingClientRect = function () {
    if (this.matches("[data-card-group-drop]")) return rect(0, 0, 100, 50);
    if (this.matches("[data-card-pool-drop]")) return rect(300, 0, 200, 50);
    if (this.matches(".account-grid")) return rect(0, 100, 500, 420);
    const card = this.closest("[data-card-id]");
    if (card) return rect(Array.from(card.parentElement.children).indexOf(card) * 250, 100, 200, 420);
    return rect(0, 0, 600, 650);
  };
  window.HTMLElement.prototype.setPointerCapture = function (id) { this.capturedPointer = id; };
  window.HTMLElement.prototype.releasePointerCapture = function () { this.capturedPointer = null; };
  const createRange = window.document.createRange.bind(window.document);
  window.document.createRange = () => {
    const range = createRange();
    range.getClientRects = () => [rect(10, 110, 80, 18)];
    return range;
  };
  const context = vm.createContext({ window, document: window.document, Element: window.Element,
    getComputedStyle: window.getComputedStyle.bind(window), performance: window.performance,
    clearTimeout: window.clearTimeout.bind(window), requestAnimationFrame: window.requestAnimationFrame.bind(window),
    cancelAnimationFrame: window.cancelAnimationFrame.bind(window),
    useRef: React.useRef, useState: React.useState, useEffect: React.useEffect, useLayoutEffect: React.useLayoutEffect });
  const source = fs.readFileSync(path.join(__dirname, "useAccountCardDrag.js"), "utf8")
    .replace(/^import .*;\r?\n/gm, "").replace(/export default function /, "function ").replace(/export function /g, "function ");
  const geometry = fs.readFileSync(path.join(__dirname, "dragGeometry.js"), "utf8").replace(/^export /gm, "");
  vm.runInContext(`${geometry}\n${source}\nthis.hook=useAccountCardDrag; this.blank=isCardBlankSpace;`, context);
  const app = fs.readFileSync(path.resolve(__dirname, "../App.jsx"), "utf8");
  for (const signature of ['className="account-title"', 'className="provider-capability-grid"',
    'className="provider-usage-strip"', 'className="account-card-head account-select"', 'data-card-id={item.id}']) {
    assert.ok(app.includes(signature), `Update fixture for changed card DOM: ${signature}`);
  }
  const h = React.createElement, moves = [];
  let show, replaceGrid, refresh, setBusy, failure = false, currentDrag, pendingSave = null, hideAfterSave = false;
  function Fixture() {
    const gridRef = React.useRef(null), [visible, setVisible] = React.useState(false),
      [revision, setRevision] = React.useState(0), [tick, setTick] = React.useState(0), [busy, updateBusy] = React.useState(false),
      [ids, setIds] = React.useState(["account:a", "provider:b"]);
    show = () => setVisible(true); replaceGrid = () => setRevision(n => n + 1); refresh = () => setTick(n => n + 1); setBusy = updateBusy;
    currentDrag = context.hook({ gridRef, items: ids.map(id => ({ id })), busy,
      onMove: async payload => {
        if (pendingSave) await pendingSave;
        if (failure) throw Error("save failed"); moves.push(JSON.parse(JSON.stringify(payload)));
        if (hideAfterSave) setIds(current => current.filter(id => id !== payload.sourceId));
      } });
    const card = id => h("article", { "data-card-id": id, "data-card-group-id": "official", key: id, className: "account-card" },
      h("div", { className: "account-card-head account-select" },
        h("span", { className: "account-title" }, h("span", { className: "title-line" }, h("strong", null, "Account name")), h("small", null, "account@example.test")),
        h("button", { className: "play-source" }, h("svg", null, h("path")))),
      h("div", { className: "provider-capability-grid" }, h("span", { "data-zone": "capability" }, h("small", null, "Capability"), h("strong", null, "Supported"))),
      h("div", { className: "provider-usage-strip" }, h("span", { "data-zone": "usage" }, h("small", null, "Usage"), h("strong", null, "128"))));
    return h("section", { "data-tick": tick }, h("div", { role: "tablist" }, h("button", { "data-card-group-drop": "work", role: "tab" }, "Work")),
      h("div", { "data-card-pool-drop": "" }, "本地 API 号池"),
      h("span", { role: "status" }, currentDrag.status),
      visible && h("div", { className: "account-grid", ref: gridRef, key: revision }, (currentDrag.previewIds || ids).filter(id => ids.includes(id)).map(card)));
  }
  const root = createRoot(window.document.getElementById("root"));
  const first = () => window.document.querySelector('[data-card-id="account:a"]');
  const overlayCount = () => window.document.querySelectorAll(".card-drag-overlay").length;
  const pointer = (target, name, overrides = {}) => {
    const event = new window.MouseEvent(name, { bubbles: true, cancelable: true, clientX: 160, clientY: 120, button: 0, buttons: name === "pointerup" ? 0 : 1, ...overrides });
    Object.defineProperties(event, { pointerType: { value: overrides.pointerType || "mouse" }, pointerId: { value: 1 }, isPrimary: { value: overrides.isPrimary ?? true } });
    target.dispatchEvent(event);
  };
  const hold = async target => act(async () => { pointer(target, "pointerdown"); pointer(target, "pointermove", {clientX:164}); });
  const cancel = async () => act(async () => { window.document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
  try {
    await act(async () => root.render(h(React.StrictMode, null, h(Fixture))));
    assert.equal(first(), null, "grid ref starts null while hook effects already mounted");
    await act(async () => show());
    await act(async () => { pointer(first(), "pointerdown"); pointer(first(), "pointermove", {clientX:162}); });
    assert.equal(overlayCount(), 0, "two-pixel click jitter does not start a drag");
    await act(async () => pointer(first(), "pointerup"));
    for (const overrides of [{button:2}, {pointerType:"touch"}, {isPrimary:false}]) {
      await act(async () => { pointer(first(), "pointerdown", overrides); pointer(first(), "pointermove", {clientX:170,...overrides}); });
      assert.equal(overlayCount(), 0, "only primary left mouse can start");
    }
    // The old tag whitelist rejects this span even though it contains no text
    // node at the point and is a stretched flex layout cell in the real CSS.
    for (const selector of [".title-line", "[data-zone=capability]", "[data-zone=usage]"]) {
      const target = first().querySelector(selector);
      assert.equal(target.tagName, "SPAN");
      assert.equal(target.matches("div,header,footer,section,article"), false, "old whitelist rejects the actual blank target");
      await hold(target); assert.equal(overlayCount(), 1, `late mount and ${selector} padding must activate`); await cancel();
    }
    const text = first().querySelector("strong");
    assert.equal(context.blank(text, first(), { clientX: 20, clientY: 120 }), false, "rendered text stays selectable");
    assert.equal(context.blank(text, first(), { clientX: 160, clientY: 120 }), true, "empty end of stretched text cell is draggable");
    await hold(first().querySelector("button path")); assert.equal(overlayCount(), 0, "nested SVG button target is excluded");
    // Do not treat a descriptive role or a focusable article as an action.
    first().setAttribute("role", "group"); first().tabIndex = 0;
    await hold(first().querySelector(".title-line")); assert.equal(overlayCount(), 1); await cancel();
    first().setAttribute("role", "button");
    await hold(first().querySelector(".title-line")); assert.equal(overlayCount(), 0);
    first().removeAttribute("role"); first().removeAttribute("tabindex");
    const oldCard = first();
    await act(async () => replaceGrid()); assert.equal(oldCard.isConnected, false);
    await hold(first().querySelector(".title-line")); assert.equal(overlayCount(), 1, "replacement grid must activate with unchanged IDs");
    for (const [y, docked] of [[60, false], [51, false], [50, true], [49, true], [51, false]]) {
      await act(async () => { pointer(first(), "pointermove", { clientX: 430, clientY: y }); await new Promise(resolve => setTimeout(resolve, 35)); });
      assert.equal(window.document.querySelector(".card-drag-overlay").classList.contains("card-drag-docked"), docked, `API pool docking follows cursor boundary at y=${y}`);
    }
    await act(async () => { pointer(first(), "pointermove", {clientX:430,clientY:160}); await new Promise(resolve => setTimeout(resolve, 35)); });
    assert.deepEqual(Array.from(currentDrag.previewIds), ["provider:b", "account:a"], "preview order changes before mouse release");
    assert.deepEqual([...window.document.querySelectorAll(".account-grid [data-card-id]")].map(node => node.dataset.cardId), ["provider:b", "account:a"]);
    assert.equal(moves.length, 0, "hover never persists");
    await act(async () => { pointer(first(), "lostpointercapture"); await new Promise(resolve => setTimeout(resolve, 25)); });
    assert.equal(overlayCount(), 1, "React keyed DOM relocation losing capture must not cancel preview");
    assert.equal(first().capturedPointer, 1, "capture is restored after React relocation");
    await act(async () => refresh()); assert.equal(overlayCount(), 1, "background rerender must not tear down the active drag");
    await act(async () => pointer(first(), "pointerup", { clientX: 430, clientY: 160 }));
    assert.deepEqual(moves[0], { sourceId: "account:a", visibleIds: ["account:a", "provider:b"], afterId: "provider:b" });
    assert.equal(overlayCount(), 0);
    await hold(first().querySelector(".title-line"));
    await act(async () => pointer(first(), "pointerup", { clientX: 40, clientY: 25 }));
    assert.equal(moves[1].groupId, "work");
    await hold(first()); await act(async () => setBusy(true)); assert.equal(overlayCount(), 0, "busy transition cancels drag");
    await act(async () => setBusy(false));
    failure = true;
    await act(async () => { assert.equal(await currentDrag.moveBy("account:a", 1), false); });
    assert.match(currentDrag.status, /移动未保存/); assert.equal(moves.length, 2);
    await hold(first());
    await act(async () => { pointer(first(), "pointermove", {clientX:430,clientY:160}); await new Promise(resolve => setTimeout(resolve, 30)); });
    assert.ok(currentDrag.previewIds);
    await act(async () => pointer(first(), "pointerup", {clientX:430,clientY:160}));
    assert.equal(currentDrag.previewIds, null, "failed save restores the declarative base order");
    assert.match(currentDrag.status, /移动未保存/); assert.equal(moves.length, 2);
    failure = false; reduceMotion = false;
    let resolvePositionSave;
    pendingSave = new Promise(resolve => { resolvePositionSave = resolve; });
    animations.length = 0;
    await hold(first());
    await act(async () => { pointer(first(), "pointermove", {clientX:430,clientY:160}); await new Promise(resolve => setTimeout(resolve, 30)); });
    window.document.querySelector(".card-drag-overlay").style.transform = "matrix(1, 0, 0, 1, 50, 60)";
    await act(async () => { pointer(first(), "pointerup", {clientX:430,clientY:160}); await new Promise(resolve => setTimeout(resolve, 30)); });
    assert.equal(currentDrag.saving, true, "persistence is still deliberately pending");
    assert.ok(animations.some(record => record.frames[0].transform === "matrix(1, 0, 0, 1, 50, 60)" && record.frames.at(-1).opacity === 1), "landing starts from the rendered position before persistence completes");
    assert.equal(overlayCount(), 0, "completed landing no longer leaves a frozen floating card while saving");
    assert.deepEqual(Array.from(currentDrag.previewIds), ["provider:b", "account:a"], "the landed preview is kept until acknowledgement");
    assert.equal(first().classList.contains("card-drag-placeholder"), false, "handoff reveals the real card without a placeholder fade");
    failure = true;
    await act(async () => { resolvePositionSave(); await pendingSave; await new Promise(resolve => setTimeout(resolve, 30)); }); pendingSave = null;
    assert.equal(currentDrag.previewIds, null, "late save failure restores the original order");
    assert.match(currentDrag.status, /移动未保存/); assert.equal(moves.length, 2);
    failure = false;
    await hold(first());
    await act(async () => { pointer(first(), "pointercancel"); await new Promise(resolve => setTimeout(resolve, 25)); });
    assert.equal(overlayCount(), 0, "pointer cancellation restores the source without saving");
    assert.equal(moves.length, 2);
    let resolveSave;
    pendingSave = new Promise(resolve => { resolveSave = resolve; });
    await hold(first());
    await act(async () => pointer(first(), "pointerup", {clientX:400,clientY:25}));
    assert.equal(currentDrag.saving, true); assert.equal(overlayCount(), 1);
    assert.equal(window.document.querySelector("[data-card-pool-drop]").classList.contains("card-drop-saving"), true);
    assert.equal(animations.some(record => record.frames.at(-1).opacity === 0), false, "ghost must not be absorbed before save succeeds");
    await act(async () => { resolveSave(); await pendingSave; }); pendingSave = null;
    assert.equal(moves[2].dropTarget, "apiPool");
    assert.ok(animations.some(record => record.frames.at(-1).opacity === 0), "successful target drop plays the absorption animation");
    assert.equal(overlayCount(), 0); assert.ok(first(), "all-groups view restores the visible source after absorption");
    hideAfterSave = true;
    await hold(first());
    await act(async () => pointer(first(), "pointerup", {clientX:40,clientY:25}));
    assert.equal(first(), null, "filtered-group refresh may remove the moved card during save");
    assert.equal(overlayCount(), 0); assert.match(currentDrag.status, /移入分组/);
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    global.window = previous.window; global.document = previous.document; global.IS_REACT_ACT_ENVIRONMENT = previous.flag;
  }
});
