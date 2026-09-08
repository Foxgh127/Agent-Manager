import assert from "node:assert/strict";
import test from "node:test";
import {
  DEFAULT_USAGE_RANGE,
  USAGE_RANGE_PREFERENCE_PATH,
  beijingDateKey,
  filterUsageRecordsByRange,
  isUsageDateKey,
  normalizeUsageRange,
  resolveUsageRange,
  serializeUsageRangePreference,
  usageRecordDateKey,
} from "./usageRange.js";

const BEIJING_SEPTEMBER_7 = new Date("2026-09-06T16:05:00Z");

test("default range is the inclusive last seven Beijing calendar days", () => {
  assert.equal(USAGE_RANGE_PREFERENCE_PATH, "appBehavior.usageRange");
  assert.equal(beijingDateKey(BEIJING_SEPTEMBER_7), "2026-09-07");
  assert.deepEqual(resolveUsageRange(undefined, BEIJING_SEPTEMBER_7), {
    ...DEFAULT_USAGE_RANGE,
    startDate: "2026-09-01",
    endDate: "2026-09-07",
    valid: true,
    error: "",
  });
  assert.deepEqual(
    resolveUsageRange({ mode: "today" }, BEIJING_SEPTEMBER_7),
    {
      schemaVersion: 1,
      mode: "today",
      customStart: "",
      customEnd: "",
      startDate: "2026-09-07",
      endDate: "2026-09-07",
      valid: true,
      error: "",
    },
  );
  assert.equal(resolveUsageRange({ mode: "month" }, BEIJING_SEPTEMBER_7).startDate, "2026-09-01");
  assert.deepEqual(
    [
      resolveUsageRange({ mode: "last7" }, new Date("2027-01-02T08:00:00Z")).startDate,
      resolveUsageRange({ mode: "last7" }, new Date("2027-01-02T08:00:00Z")).endDate,
    ],
    ["2026-12-27", "2027-01-02"],
  );
});

test("custom range validates real dates and chronological order", () => {
  assert.equal(isUsageDateKey("2028-02-29"), true);
  assert.equal(isUsageDateKey("2026-02-29"), false);
  assert.equal(resolveUsageRange({ mode: "custom", customStart: "2026-08-31", customEnd: "2026-09-02" }).valid, true);
  assert.match(
    resolveUsageRange({ mode: "custom", customStart: "2026-09-03", customEnd: "2026-09-02" }).error,
    /开始日期/,
  );
  assert.throws(
    () => serializeUsageRangePreference({ mode: "custom", customStart: "", customEnd: "2026-09-02" }),
    /有效/,
  );
});

test("normalization keeps custom dates while presets change across data sources", () => {
  const prior = normalizeUsageRange({ mode: "custom", startDate: "2026-08-01", endDate: "2026-08-10" });
  const preset = normalizeUsageRange({ ...prior, mode: "last7" });
  assert.equal(preset.customStart, "2026-08-01");
  assert.equal(preset.customEnd, "2026-08-10");
  assert.deepEqual(serializeUsageRangePreference(preset, BEIJING_SEPTEMBER_7), {
    schemaVersion: 1,
    mode: "last7",
    customStart: "2026-08-01",
    customEnd: "2026-08-10",
  });
  assert.equal(normalizeUsageRange({ mode: "unsupported" }).mode, "last7");
});

test("record dates use explicit day keys first and Beijing time for timestamps", () => {
  assert.equal(usageRecordDateKey({ date: "2026-09-01" }), "2026-09-01");
  assert.equal(usageRecordDateKey({ timestamp: "2026-09-01T16:30:00Z" }), "2026-09-02");
  assert.equal(usageRecordDateKey({ date: "invalid", timestamp: "invalid" }), "");
});

test("range filtering is inclusive and invalid custom ranges show no misleading totals", () => {
  const records = [
    { date: "2026-08-31", tokens: 1 },
    { date: "2026-09-01", tokens: 2 },
    { date: "2026-09-07", tokens: 3 },
    { date: "2026-09-08", tokens: 4 },
  ];
  assert.deepEqual(
    filterUsageRecordsByRange(records, { mode: "last7" }, BEIJING_SEPTEMBER_7).map((item) => item.tokens),
    [2, 3],
  );
  assert.deepEqual(
    filterUsageRecordsByRange(records, { mode: "custom", customStart: "", customEnd: "" }),
    [],
  );
});
