const { test }=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const React=require('react');const {JSDOM}=require('jsdom');

test('behavior settings contain four cells and exit-only uses one fresh acknowledged decision',async()=>{
 const dom=new JSDOM('<div id="root"></div>');
 const previous={window:global.window,document:global.document,flag:global.IS_REACT_ACT_ENVIRONMENT};
 Object.assign(global,{window:dom.window,document:dom.window.document,IS_REACT_ACT_ENVIRONMENT:true});
 const {transformWithOxc}=await import('vite');
 const calls=[],decisions=[],notices=[];let finishDecision,scheduled,reloads=0;
 dom.window.setTimeout=callback=>{scheduled=callback;return 1;};dom.window.clearTimeout=()=>{};
 const context=vm.createContext({React,window:dom.window,Date,
   ...Object.fromEntries(['useState','useEffect','useRef','useCallback'].map(name=>[name,React[name]])),
   ...Object.fromEntries(['FolderOpen','Link2','Loader2','RefreshCw','Settings','LogOut','X'].map(name=>[name,()=>null])),
   AppearanceControls:()=>null,UpdateEmergencyPanel:()=>null,RecoveryPanel:()=>null,
   Modal:({children})=>React.createElement('section',{role:'dialog'},children),
   Switch:()=>React.createElement('input',{type:'checkbox','aria-label':'关闭后最小化到系统托盘'}),formatDateTime:value=>value,
   api:async(route,options)=>{
     calls.push({route,body:options?.body && JSON.parse(options.body)});
     if(route==='/api/application/location')return {location:{supported:true,directory:'D:\\工具',executable:'D:\\工具\\AgentManager.exe'}};
     if(route==='/api/exit-only/preflight')return {preflight:{requiresConfirmation:true,message:'当前本地路由会停止，确认后继续'}};
     if(route==='/api/app-lifecycle')return {shutdown:{phase:'blocked-services',at:new Date().toISOString(),errors:['组件仍在退出，请稍后重试']}};
     return {ok:true};
   }});
 const compile=async(name,source)=>vm.runInContext((await transformWithOxc(source,name,{jsx:{runtime:'classic'}})).code,context);
 await compile('ApplicationLocationPanel.jsx',fs.readFileSync(path.join(__dirname,'ApplicationLocationPanel.jsx'),'utf8')
   .replace(/^import .*;\r?\n/gm,'').replace('export default function','function'));
 const app=fs.readFileSync(path.join(__dirname,'../App.jsx'),'utf8');
 await compile('SettingsView.jsx',app.slice(app.indexOf('function SettingsView('),app.indexOf('function ClaudeBrandMark(')));
 const {createRoot}=require('react-dom/client');const root=createRoot(document.querySelector('#root'));
 const data={settings:{appBehavior:{closeToTray:true}},web2apiStatus:{running:false}};
 const button=text=>[...document.querySelectorAll('button')].find(item=>item.textContent===text);
 const confirm=details=>{decisions.push(details);return new Promise(resolve=>{finishDecision=resolve;});};
 function Wrapper(){const [busy,setBusy]=React.useState(false);return React.createElement(context.SettingsView,{data,confirm,busy,setBusy,notify:(...args)=>notices.push(args),reload:async()=>{reloads++;}});}
 try{
   await React.act(async()=>root.render(React.createElement(Wrapper)));
   const card=document.querySelector('.settings-behavior-card');
   assert.equal(card.querySelectorAll(':scope > .setting-row').length,4);
   assert.equal(document.querySelector('.application-location-card'),null);
   assert.match(card.querySelector(':scope > .setting-row:last-child').textContent,/应用位置.*D:\\工具\\AgentManager.exe/);
   await React.act(async()=>{button('仅退出软件').click();button('仅退出软件').click();});
   assert.equal(decisions.length,1);
   assert.match(decisions[0].detail,/当前本地路由/);
   assert.equal(calls.filter(c=>c.route==='/api/exit-only').length,0);
   await React.act(async()=>finishDecision(true));
   assert.deepEqual(calls.find(c=>c.route==='/api/exit-only').body,{confirmed:true});
   await React.act(async()=>scheduled());
   assert.deepEqual(notices.at(-1),['组件仍在退出，请稍后重试','error']);
   assert.equal(reloads,1);
   await React.act(async()=>button('彻底退出').click());
   await React.act(async()=>finishDecision(false));
   assert.equal(calls.filter(c=>c.route==='/api/shutdown').length,0);
   await React.act(async()=>button('快速重启').click());
   await React.act(async()=>finishDecision(false));
   assert.equal(calls.filter(c=>c.route==='/api/quick-restart').length,0);
 }finally{
   await React.act(async()=>root.unmount());dom.window.close();
   Object.assign(global,{window:previous.window,document:previous.document,IS_REACT_ACT_ENVIRONMENT:previous.flag});
 }
});
