import assert from "node:assert/strict";
import test from "node:test";
import { updateFailureMessage } from "./updateFeedback.js";

test("update metadata failures explain that the published manifest is invalid", () => {
  assert.equal(
    updateFailureMessage("更新元数据超过读取上限。"),
    "发布方的更新清单无效，请稍后重试",
  );
  assert.equal(
    updateFailureMessage("metadata_too_large"),
    "发布方的更新清单无效，请稍后重试",
  );
});

test("GitHub throttling is shown as a temporary update-source failure", () => {
  assert.equal(updateFailureMessage("更新源返回 HTTP 403。"), "更新源暂时受限，请稍后重试");
  assert.equal(updateFailureMessage("HTTP 429 rate limit"), "更新源暂时受限，请稍后重试");
});
