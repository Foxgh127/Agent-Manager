import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { loadAppUpdate } from "./appUpdateResource.js";

const source = readFileSync(new URL("./App.jsx", import.meta.url), "utf8").replaceAll("\r\n", "\n");

test("application startup checks maintenance exactly once", async () => {
  const start = source.indexOf("function refreshStartupResourcesOnce()");
  const end = source.indexOf("function cx(", start);
  const calls = [];
  const context = vm.createContext({
    api: async (url) => { calls.push(url); return { cached: true, status: { configured: true } }; },
    loadAppUpdate,
    startupResourceRefreshPromise: null,
    maintenanceStartupPromise: null,
    radarResourceCache: {}, maintenanceResourceCache: null, skillsResourceCache: null,
  });
  vm.runInContext(source.slice(start, end), context);
  const first = context.refreshStartupResourcesOnce();
  assert.equal(context.refreshStartupResourcesOnce(), first);
  await first;
  assert.deepEqual(calls, ["/api/updates/check", "/api/emergency/checks?force=1", "/api/app-update", "/api/radar", "/api/app-update/check", "/api/skills", "/api/skills/catalog"]);
  await context.refreshStartupResourcesOnce();
  assert.equal(calls.length, 7);
});

test("activation completion replaces the pre-activation dashboard once", async () => {
  const start = source.indexOf("  useEffect(() => {\n    const session = data?.configurationSession;");
  const end = source.indexOf("  // Radar, skill catalog", start);
  let request;
  let reloads = 0;
  const context = vm.createContext({
    data: { configurationSession: { status: "starting", active: false } },
    api: async () => ({ configurationSession: { status: "active", active: true } }),
    reload: async () => { reloads++; },
    updateData: () => { throw Error("must replace stale dashboard"); },
    maintenanceResourceCache: { stale: true },
    useEffect: callback => callback(),
    window: { setInterval: callback => { request = callback; return 1; }, clearInterval() {} },
  });
  vm.runInContext(source.slice(start, end), context);
  await request(); // while the immediate call is in flight, no duplicate
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(reloads, 1);
  assert.equal(context.maintenanceResourceCache.stale, true);
});
