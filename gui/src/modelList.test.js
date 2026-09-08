import { test } from "node:test";
import assert from "node:assert/strict";
import { listedModels } from "./modelList.js";

test("model list preserves actual IDs across provider strings and structured sources", () => {
  assert.deepEqual(listedModels([" gpt-6-astra ", {id:"gpt-6-astra"}, {id:"provider/model",displayName:"供应商模型"}, null, ""]), [
    {id:"gpt-6-astra",label:"gpt-6-astra"}, {id:"provider/model",label:"供应商模型"},
  ]);
  assert.deepEqual(listedModels(null), []);
});
