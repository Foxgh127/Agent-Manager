const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const React = require('react');

test('one-click update checks, downloads, verifies, then installs exactly once', async () => {
  const dom = new JSDOM('<div id="root"></div>');
  const previous = { window: global.window, document: global.document, flag:global.IS_REACT_ACT_ENVIRONMENT };
  global.window=dom.window;global.document=dom.window.document;global.IS_REACT_ACT_ENVIRONMENT=true;
  let poll;
  dom.window.setInterval = callback => {poll=callback;return 1;};
  dom.window.clearInterval = () => {};
  const { transformWithOxc } = await import('vite');
  const source = fs.readFileSync(path.join(__dirname,'AppUpdatePanel.jsx'),'utf8').replace(/^\uFEFF/,'')
    .replace(/^import .*;\r?\n/gm,'').replace('export default function','function');
  const transformed = await transformWithOxc(source,'AppUpdatePanel.jsx',{jsx:{runtime:'classic'}});
  const context=vm.createContext({React,window:dom.window,useRef:React.useRef,useState:React.useState,
    useEffect:React.useEffect,useCallback:React.useCallback,Check:()=>null,Download:()=>null,Loader2:()=>null,RefreshCw:()=>null,
    ...(await import('../appUpdateResource.js')), ...(await import('../updateFeedback.js'))});
  const actionSource = fs.readFileSync(path.join(__dirname,'UpdateAction.jsx'),'utf8')
    .replace(/^import .*;\r?\n/gm,'').replace('export default function','function');
  vm.runInContext((await transformWithOxc(actionSource,'UpdateAction.jsx',{jsx:{runtime:'classic'}})).code, context);
  vm.runInContext(transformed.code+'\nthis.Panel=AppUpdatePanel;',context);
  const { createRoot } = require('react-dom/client');const root=createRoot(document.querySelector('#root'));
  const calls=[];
  let status={currentVersion:'9.13.0',configured:true,state:'update_available',updateAvailable:true,
    canDownload:true,installSupported:true,download:{state:'idle'},latestRelease:{version:'9.14.0',releaseToken:'fixture'}};
  const api=async (route,options)=>{
    calls.push(route);
    if(route.endsWith('/download')) {assert.equal(JSON.parse(options.body).releaseToken,'fixture');status={...status,canDownload:false,download:{state:'downloading',totalBytes:100,downloadedBytes:0}};}
    if(route.endsWith('/install'))return {message:'ready to restart'};
    return {status};
  };
  try {
    await React.act(async()=>root.render(React.createElement(context.Panel,{api,notify:()=>{}})));
    assert.deepEqual(calls,['/api/app-update']);
    await React.act(async()=>root.render(null));
    await React.act(async()=>root.render(React.createElement(context.Panel,{api,notify:()=>{}})));
    assert.deepEqual(calls,['/api/app-update']); // reopening settings does not check again
    assert.match(document.body.textContent,/9.13.0/);
    assert.doesNotMatch(document.body.textContent,/更新源设置|下载目录/);
    await React.act(async()=>root.render(React.createElement(context.Panel,{api,notify:()=>{},refreshing:true})));
    assert.equal(document.querySelector('.update-action').textContent, '检查中');
    assert.equal(document.querySelector('.update-action').disabled, true);
    assert.equal(document.querySelector('.update-copy [title]'), null);
    await React.act(async()=>root.render(React.createElement(context.Panel,{api,notify:()=>{}})));
    await React.act(async()=>document.querySelector('button').click());
    assert.equal(calls.filter(v=>v.endsWith('/install')).length,0);
    status={...status,canInstall:true,download:{state:'ready',verified:true}};
    await React.act(async()=>poll());
    assert.equal(calls.filter(v=>v.endsWith('/install')).length,1);
    status={...status,canInstall:false,canDownload:false,state:'current',updateAvailable:false,
      installation:{state:'installed',code:'startup_unverified'},download:{state:'idle'}};
    await React.act(async()=>poll());
    assert.match(document.body.textContent,/更新已安装，启动状态尚未确认/);
    assert.equal(document.querySelector('.update-action').disabled,false);
    assert.doesNotMatch(document.body.textContent,/重启中/);
    status={...status,installation:{state:'complete',code:'update_ready_reconciled'}};
    await React.act(async()=>document.querySelector('.update-action').click());
    assert.match(document.body.textContent,/已是最新版/);
    await React.act(async()=>poll());
    assert.equal(calls.filter(v=>v.endsWith('/install')).length,1);
  } finally {
    await React.act(async()=>root.unmount());dom.window.close();
    global.window=previous.window;global.document=previous.document;global.IS_REACT_ACT_ENVIRONMENT=previous.flag;
  }
});
