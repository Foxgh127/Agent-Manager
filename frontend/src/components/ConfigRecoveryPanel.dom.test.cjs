const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { JSDOM } = require("jsdom");
const React = require("react");
const { act } = React;

test("configuration backups paginate, save once, delete by ID and retain recovery controls", async () => {
  const dom = new JSDOM("<!doctype html><body><div id='root'></div></body>");
  const previous = { window: global.window, document: global.document, flag: global.IS_REACT_ACT_ENVIRONMENT };
  global.window = dom.window; global.document = dom.window.document; global.IS_REACT_ACT_ENVIRONMENT = true;
  const { createRoot } = require("react-dom/client");
  const { transformWithOxc } = await import("vite");
  const source = fs.readFileSync(path.join(__dirname, "ConfigRecoveryPanel.jsx"), "utf8")
    .replace(/^import .*;\r?\n/gm, "").replace("export default function", "function");
  const transformed = await transformWithOxc(source, "ConfigRecoveryPanel.jsx", { jsx: { runtime: "classic" } });
  const context = vm.createContext({ React, useRef: React.useRef, useState: React.useState,
    Archive: () => null, ChevronLeft: () => null, ChevronRight: () => null,
    Loader2: () => null, RotateCcw: () => null, ShieldCheck: () => null, Trash2: () => null,
    Modal: ({ children }) => React.createElement("section", { role: "dialog" }, children) });
  vm.runInContext(transformed.code + "\nthis.Panel=ConfigRecoveryPanel;", context);
  const root = createRoot(document.querySelector("#root"));
  let rows = Array.from({ length: 13 }, (_, index) => ({ id: `id-${index}`, name: `config.toml.long-file-name-${index}.bak`,
    fingerprint: `hash-${index}`, size: 2048, modifiedAt: 1788890000 - index,
    kind: index === 0 ? "external" : index === 1 ? "recovery" : "auto", canRestore: index !== 1,
    canDelete: index !== 0, protectedReason: index === 0 ? "外部备份由原工具管理" : "" }));
  const inspection = () => ({ fingerprint: "current-fingerprint", exists: true, status: "encoding", canNormalize: true,
    message: "配置编码可无损转换为 UTF-8。", backups: rows.filter(row => row.canRestore), backupFiles: rows, automaticLimit: 3 });
  const requests = [], confirmations = [], notices = [];
  let finishManual, restored = 0;
  const button = text => [...document.querySelectorAll("button")].find(item => item.textContent === text);
  try {
    await act(async () => root.render(React.createElement(context.Panel, {
      hasUnsavedDraft: true, notify: (...args) => notices.push(args), onRestored: () => { restored++; },
      confirm: async payload => { confirmations.push(payload); return true; },
      api: async (url, options) => {
        requests.push({ url, body: options && JSON.parse(options.body) });
        if (!options) return { inspection: inspection() };
        if (url.endsWith("/backups")) return new Promise(resolve => { finishManual = () => {
          rows = [{ id: "manual-id", name: "manual-copy.bak", kind: "manual", size: 300, modifiedAt: 1788900000,
            canRestore: true, canDelete: true, fingerprint: "manual-hash" }, ...rows];
          resolve({ result: { id: "manual-id" }, inspection: inspection() });
        }; });
        if (url.endsWith("/delete")) { rows = rows.filter(row => row.id !== JSON.parse(options.body).backupId); return { result: { deleted: true }, inspection: inspection() }; }
        return { result: { changed: true, warning: "配置已恢复，部分备份受保护而保留" } };
      },
    })));
    await act(async () => button("恢复配置").click());
    assert.equal(document.querySelectorAll(".config-backup-row").length, 5);
    assert.equal(document.querySelector("select"), null);
    assert.match(document.querySelector('[role="dialog"]').textContent, /未保存的修改不会包含/);
    assert.ok(button("无损转换为 UTF-8"));
    assert.ok(button("恢复所选备份"));
    assert.equal(document.querySelector('[aria-label="删除备份 config.toml.long-file-name-0.bak"]').disabled, true);
    assert.equal(document.querySelector('[aria-label="选择备份 config.toml.long-file-name-1.bak"]').disabled, true);
    await act(async () => document.querySelector('[aria-label="下一页备份"]').click());
    assert.match(document.querySelector(".config-backup-name").textContent, /name-5/);
    await act(async () => { button("手动备份").click(); button("手动备份").click(); });
    assert.equal(requests.filter(row => row.url.endsWith("/backups")).length, 1);
    assert.equal(button("手动备份").disabled, true);
    assert.deepEqual(requests.at(-1).body, { expectedFingerprint: "current-fingerprint" });
    await act(async () => finishManual());
    assert.equal(document.querySelector('[aria-label="选择备份 manual-copy.bak"]').checked, true);
    assert.equal(restored, 0);
    await act(async () => document.querySelector('[aria-label="删除备份 manual-copy.bak"]').click());
    assert.deepEqual(requests.at(-1).body, { backupId: "manual-id" });
    assert.equal(document.querySelector('[aria-label="选择备份 manual-copy.bak"]'), null);
    assert.equal(confirmations.length, 1);
    await act(async () => button("恢复所选备份").click());
    assert.deepEqual(requests.at(-1).body, { expectedFingerprint: "current-fingerprint", backupId: "id-0", reset: false });
    assert.equal(restored, 1);
    assert.equal(document.querySelector('[role="dialog"]'), null);
    assert.equal(notices.length, 3);
    assert.deepEqual(notices.at(-1), ["配置已恢复，部分备份受保护而保留", "warning"]);
  } finally {
    await act(async () => root.unmount());
    global.window = previous.window; global.document = previous.document; global.IS_REACT_ACT_ENVIRONMENT = previous.flag;
    dom.window.close();
  }
});
