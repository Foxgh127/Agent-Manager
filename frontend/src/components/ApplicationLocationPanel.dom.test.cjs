const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const React = require('react');
const { JSDOM } = require('jsdom');

test('application location preserves Unicode, cancels safely and submits each move/shortcut once', async () => {
  const dom=new JSDOM('<div id="root"></div>');
  const previous={window:global.window,document:global.document,flag:global.IS_REACT_ACT_ENVIRONMENT};
  Object.assign(global,{window:dom.window,document:dom.window.document,IS_REACT_ACT_ENVIRONMENT:true});
  let poll;
  dom.window.setInterval=callback=>{poll=callback;return 1;};dom.window.clearInterval=()=>{};
  const { transformWithOxc }=await import('vite');
  const source=fs.readFileSync(path.join(__dirname,'ApplicationLocationPanel.jsx'),'utf8')
    .replace(/^import .*;\r?\n/gm,'').replace('export default function','function');
  const context=vm.createContext({React,window:dom.window,Date,useState:React.useState,useEffect:React.useEffect,useCallback:React.useCallback,useRef:React.useRef,
    FolderOpen:()=>null,Link2:()=>null,Loader2:()=>null,RefreshCw:()=>null,
    Modal:({children,title})=>React.createElement('section',{role:'dialog','aria-label':title},children)});
  vm.runInContext((await transformWithOxc(source,'ApplicationLocationPanel.jsx',{jsx:{runtime:'classic'}})).code+'\nthis.Panel=ApplicationLocationPanel;',context);
  const { createRoot }=require('react-dom/client');const root=createRoot(document.querySelector('#root'));
  const sourceDirectory="C:\\用户 [测试]\\微信 & 安装";
  const destination="D:\\应用 程序\\张三's [文件夹]";
  let location={supported:true,canBrowse:true,directory:sourceDirectory,executable:sourceDirectory+'\\AgentManager.exe'};
  let finishMove,finishShortcut;const calls=[],notices=[];
  const api=async(route,options)=>{
    calls.push({route,body:options?.body && JSON.parse(options.body)});
    if(route.endsWith('/select'))return{directory:destination};
    if(route.endsWith('/move'))return new Promise(resolve=>{finishMove=()=>resolve({result:{started:true}});});
    if(route.endsWith('/shortcut'))return new Promise(resolve=>{finishShortcut=()=>resolve({message:'已创建'});});
    return{location};
  };
  const button=text=>[...document.querySelectorAll('button')].find(item=>item.textContent===text);
  try{
    await React.act(async()=>root.render(React.createElement(context.Panel,{api,notify:(...args)=>notices.push(args)})));
    assert.match(document.body.textContent,/用户 \[测试\]/);
    await React.act(async()=>button('更改位置').click());
    await React.act(async()=>button('选择文件夹').click());
    assert.equal(document.querySelector('input').value,destination);
    await React.act(async()=>button('取消').click());
    assert.equal(calls.filter(c=>c.route.endsWith('/move')).length,0);
    await React.act(async()=>{button('创建桌面快捷方式').click();button('创建桌面快捷方式').click();});
    assert.equal(calls.filter(c=>c.route.endsWith('/shortcut')).length,1);
    await React.act(async()=>finishShortcut());
    await React.act(async()=>button('更改位置').click());
    await React.act(async()=>button('选择文件夹').click());
    await React.act(async()=>{button('移动并重启').click();button('移动并重启').click();});
    assert.deepEqual(calls.filter(c=>c.route.endsWith('/move')).map(c=>c.body),[{directory:destination}]);
    await React.act(async()=>finishMove());
    assert.match(document.body.textContent,/移动中/);
    location={...location,relocation:{state:'failed',message:'目标文件已存在，未移动'}};
    await React.act(async()=>poll());
    assert.match(document.body.textContent,/目标文件已存在/);
    assert.equal(button('更改位置').disabled,false);
  }finally{
    await React.act(async()=>root.unmount());dom.window.close();
    Object.assign(global,{window:previous.window,document:previous.document,IS_REACT_ACT_ENVIRONMENT:previous.flag});
  }
});

test('settings omit the three removed status rows while preserving the actual tray control',()=>{
  const source=fs.readFileSync(path.join(__dirname,'../App.jsx'),'utf8');
  assert.doesNotMatch(source,/子代理临时配置|托盘运行状态|静默运行外部命令/);
  assert.match(source,/关闭后最小化到系统托盘/);
});
