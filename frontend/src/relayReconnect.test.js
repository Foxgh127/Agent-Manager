import { test } from "node:test";
import assert from "node:assert/strict";
import { relayReconnectTarget } from "./relayReconnect.js";

test("website reconnect accepts only explicit HTTPS www/apex counterparts and omits URL credentials", () => {
  const value={savedOrigin:"https://www.example.test",currentOrigin:"https://example.test",reconnectUrl:"https://example.test/dashboard?token=not-carried#not-carried"};
  assert.equal(relayReconnectTarget(value),"https://example.test");
  for (const reconnectUrl of ["https://evil.test", "http://example.test", "https://user:password@example.test", "javascript:alert(1)"]) {
    assert.equal(relayReconnectTarget({...value,reconnectUrl}),"");
  }
  assert.equal(relayReconnectTarget({...value,currentOrigin:"https://login.example.test",reconnectUrl:"https://login.example.test"}),"");
});
