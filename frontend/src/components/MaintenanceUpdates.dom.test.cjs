const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const React = require('react');

test('refresh keeps all three update cards consistent until every check settles, without raw tooltips', async () => {
  const dom = new JSDOM('<div id="root"></div>');
  const previous = { window:global.window, document:global.document, flag:global.IS_REACT_ACT_ENVIRONMENT };
  global.window=dom.window; global.document=dom.window.document; global.IS_REACT_ACT_ENVIRONMENT=true;
  const { transformWithOxc } = await import('vite');
  const updates = {status:{components:{desktop:{installedVersion:'26.901.6511.0',updateState:'check_failed',
    message:'Checking updates…\nWould you like to apply?\nFailed to read input in non-interactive mode.'},
    cli:{installedVersion:'0.153.4',updateAvailable:true,availableVersion:'0.154.0'}}}};
  let releaseManager;
  const manager = {configured:true,currentVersion:'1.1.2',state:'check_failed',installation:{state:'failed',
    message:'Started manager did not publish a verified ready window and service. Rollback/restart: New manager is still running; backup retained for safe manual recovery.'}};
  const calls=[];
  const api=async route => {
    calls.push(route);
    if(route==='/api/app-update/check') return new Promise(resolve => {releaseManager=()=>resolve({status:{...manager,state:'current',installation:{state:'complete'}}});});
    if(route==='/api/app-update') return {status:manager};
    if(route==='/api/updates/check') return updates;
    return {};
  };
  const context = vm.createContext({React,window:dom.window, api, ...await import('../appUpdateResource.js'),
    ...await import('../updateFeedback.js'), useRef:React.useRef,useState:React.useState,useEffect:React.useEffect,useCallback:React.useCallback,
    maintenanceResourceCache:{updates,checks:{checks:[{id:'generated_configuration',ok:true}]}},
    firstArray:(data,keys)=>keys.map(key=>data?.[key]).find(Array.isArray)||[], cx:(...items)=>items.filter(Boolean).join(' '),
    ...Object.fromEntries(['Check','Download','Loader2','RefreshCw','HardDriveDownload','AlertTriangle','Clock3','Bot','ShieldCheck','ShieldAlert','Wrench'].map(name=>[name,()=>null]))});
  const compile = async (name, source) => vm.runInContext((await transformWithOxc(source,name,{jsx:{runtime:'classic'}})).code,context);
  for(const name of ['UpdateAction.jsx','AppUpdatePanel.jsx']) {
    const source=fs.readFileSync(path.join(__dirname,name),'utf8').replace(/^import .*;\r?\n/gm,'').replace('export default function','function');
    await compile(name,source);
  }
  const app=fs.readFileSync(path.join(__dirname,'../App.jsx'),'utf8');
  await compile('Maintenance.jsx',app.slice(app.indexOf('function updateComponent('),app.indexOf('function SettingsView(')));
  const { createRoot }=require('react-dom/client');
  const root=createRoot(document.querySelector('#root'));
  const props={data:{settings:{modelWorkspace:{mode:'aggregate'}},selectedModelKeys:[],status:{fullyApplied:true}},notify:()=>{},confirm:async()=>true};
  try {
    await React.act(async()=>root.render(React.createElement(context.UpdateEmergencyPanel,props)));
    assert.equal(document.querySelector('.update-grid [title]'),null);
    assert.doesNotMatch(document.querySelector('.update-grid').textContent,/Started manager|Checking updates|non-interactive|Rollback/);
    await React.act(async()=>document.querySelector('header button').click());
    assert.equal(typeof releaseManager,'function');
    const buttons=[...document.querySelectorAll('.update-action')];
    assert.equal(buttons.length,3);
    assert.ok(buttons.every(button=>button.textContent==='检查中' && button.disabled && button.getAttribute('aria-busy')==='true'));
    assert.equal(document.querySelector('header button').textContent,'刷新中');
    await React.act(async()=>releaseManager());
    assert.equal(document.querySelector('header button').disabled,false);
    assert.ok([...document.querySelectorAll('.update-action')].every(button=>button.getAttribute('aria-busy')==='false'));
    const count=calls.length;
    await React.act(async()=>root.render(null));
    await React.act(async()=>root.render(React.createElement(context.UpdateEmergencyPanel,props)));
    assert.equal(calls.length,count);
  } finally {
    await React.act(async()=>root.unmount());dom.window.close();
    global.window=previous.window;global.document=previous.document;global.IS_REACT_ACT_ENVIRONMENT=previous.flag;
  }
});
