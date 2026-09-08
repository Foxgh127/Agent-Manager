import {test} from "node:test";
import assert from "node:assert/strict";
import {quotaIsCurrent} from "./quotaFreshness.js";
test("old zero percent is not presented as the current subscription allowance",()=>{
  const now=Date.parse("2026-09-08T00:00:00Z");
  assert.equal(quotaIsCurrent({remainingPercent:0,resetAt:"2026-08-20T00:00:00Z"},false,now),false);
  assert.equal(quotaIsCurrent({remainingPercent:0,resetAt:"2026-09-15T00:00:00Z"},false,now),true);
  assert.equal(quotaIsCurrent({remainingPercent:90},true,now),false);
  assert.equal(quotaIsCurrent({remainingPercent:90,stale:true},false,now),false);
  assert.equal(quotaIsCurrent({remainingPercent:null},false,now),false);
});
