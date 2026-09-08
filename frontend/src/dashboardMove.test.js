import test from "node:test";
import assert from "node:assert/strict";
import { applyDashboardMoveResult } from "./dashboardMove.js";

const data = () => ({
  settings: {
    accounts: [{id:"a",groupId:"official",usage:{weekly:{remainingPercent:82}}}],
    providers: [{id:"p",groupId:"relay"},{id:"child",groupId:"relay"},{id:"other",groupId:"work"}],
    relayAccounts:[{id:"r",groupId:"relay",selectedKeyId:"key"}],
    dashboardOrder:["account:a","relay:r","provider:other"],
    web2api:{accountIds:[],providerIds:[],sourceOrder:[],activeForCodex:false,routing:"ordered"},
  },
  modelSources:[{id:"account:a",kind:"account",recordId:"a",groupId:"official"},{id:"provider:p",kind:"provider",recordId:"p",groupId:"relay"}],
  web2apiStatus:{running:false,memberCount:0},auth:{activeAccountId:"a"},
});

test("order acknowledgement retains all account/model/status objects", () => {
  const before=data(); const after=applyDashboardMoveResult(before,{dashboardOrder:["relay:r","account:a","provider:other"]});
  assert.deepEqual(after.settings.dashboardOrder,["relay:r","account:a","provider:other"]);
  for (const key of ["accounts","providers","relayAccounts","web2api"]) assert.equal(after.settings[key],before.settings[key]);
  assert.equal(after.modelSources,before.modelSources); assert.equal(after.web2apiStatus,before.web2apiStatus); assert.equal(after.auth,before.auth);
});

test("compact group acknowledgement updates linked cards and model sources only", () => {
  const before=data(); const after=applyDashboardMoveResult(before,{groupAssignments:{providers:{p:"work",child:"work"},relayAccounts:{r:"work"}}});
  assert.deepEqual(after.settings.providers.map(p=>p.groupId),["work","work","work"]);
  assert.equal(after.settings.providers[2],before.settings.providers[2]);
  assert.equal(after.settings.accounts,before.settings.accounts); assert.equal(after.settings.relayAccounts[0].selectedKeyId,"key");
  assert.equal(after.modelSources[1].groupId,"work"); assert.equal(after.modelSources[0],before.modelSources[0]);
});

test("pool acknowledgement updates membership without changing dashboard or service", () => {
  const before=data(); const after=applyDashboardMoveResult(before,{dropTarget:"apiPool",pool:{accountIds:["a"],providerIds:["p"],sourceOrder:["account:a","provider:p"]}});
  assert.equal(after.settings.accounts[0].proxyEnabled,true); assert.equal(after.settings.providers[0].proxyEnabled,true);
  assert.equal(after.settings.providers[1],before.settings.providers[1]); assert.equal(after.settings.dashboardOrder,before.settings.dashboardOrder);
  assert.equal(after.web2apiStatus.memberCount,2); assert.equal(after.web2apiStatus.running,false); assert.equal(after.settings.web2api.activeForCodex,false);
});
