const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { JSDOM } = require("jsdom");
const React = require("react");
const { act } = React;

test("one click repairs without confirmation, blocks duplicate requests, and retains partial feedback", async () => {
  const dom = new JSDOM("<!doctype html><body><div id='root'></div></body>");
  const previous = { window: global.window, document: global.document, flag: global.IS_REACT_ACT_ENVIRONMENT };
  global.window = dom.window; global.document = dom.window.document; global.IS_REACT_ACT_ENVIRONMENT = true;
  const { createRoot } = require("react-dom/client");
  const { transformWithOxc } = await import("vite");
  const source = fs.readFileSync(path.join(__dirname, "SessionRepairPanel.jsx"), "utf8")
    .replace(/^import .*;\r?\n/gm, "").replace("export default function", "function");
  const transformed = await transformWithOxc(source, "SessionRepairPanel.jsx", { jsx: { runtime: "classic" } });
  const context = vm.createContext({ React, useRef: React.useRef, useState: React.useState,
    Check: () => null, ChevronDown: () => null, Loader2: () => null, Wrench: () => null,
    HistoryRecoveryPanel: () => null, SessionVisibilityPanel: () => null });
  vm.runInContext(transformed.code + "\nthis.Panel=SessionRepairPanel;", context);
  const root = createRoot(document.querySelector("#root"));
  const requests = [], notices = [], busyChanges = [];
  let complete, recovered = 0, confirmations = 0;
  try {
    await act(async () => root.render(React.createElement(context.Panel, {
      api: (...args) => { requests.push(args); return new Promise(resolve => { complete = resolve; }); },
      notify: (...args) => notices.push(args), confirm: () => { confirmations++; },
      onRecovered: async () => { recovered++; }, onBusyChange: busy => busyChanges.push(busy),
    })));
    const button = document.querySelector(".session-repair-heading button");
    await act(async () => { button.click(); button.click(); });
    assert.equal(requests.length, 1);
    assert.equal(confirmations, 0);
    assert.equal(requests[0][0], "/api/sessions/repair");
    assert.equal(requests[0][1].method, "POST");
    assert.equal(button.disabled, true);
    assert.match(document.querySelector('[role="status"]').textContent, /正在检查/);
    await act(async () => complete({ result: { status: "partial", partial: true, message: "部分项目仍需处理", warnings: ["保留外部修改"] } }));
    assert.equal(button.disabled, false);
    assert.equal(recovered, 1);
    assert.deepEqual(busyChanges, [true, false]);
    assert.equal(notices[0][1], "warning");
    assert.match(document.querySelector('[role="status"]').textContent, /保留外部修改/);
    assert.equal(document.querySelector("details").open, false);
  } finally {
    await act(async () => root.unmount());
    global.window = previous.window; global.document = previous.document; global.IS_REACT_ACT_ENVIRONMENT = previous.flag;
    dom.window.close();
  }
});
