/** Execute lifecycle/controller code with a VS Code host stub, even before view resolution. */
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import * as fs from 'node:fs';
import * as crypto from 'node:crypto';
import * as os from 'node:os';
import { tmpdir } from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';
import type { GdrSession as Session, GdrCache as Cache } from '../src/session.ts';
import type { RtosViewsPanel as Panel } from '../src/panel.ts';

function host(settings = {rtos:'',version:'',autoInit:true}) {
 const alerts: {level: string; message: string}[] = [];
 const logs: string[] = [];
 const vscode = {
  window: {
   showErrorMessage: (message: string) => alerts.push({level:'error',message}),
   showWarningMessage: (message: string) => alerts.push({level:'warning',message}),
   showQuickPick: async () => undefined
  },
  commands: {executeCommand: async () => undefined}
 };
 const cache = new Map<string, any>();
 const builtins: Record<string, unknown> = {
  'node:fs':fs, 'node:crypto':crypto, 'node:os':os, 'node:path':path
 };
 function load(file: string): any {
  const absolute = path.resolve(file);
  if (cache.has(absolute)) { return cache.get(absolute); }
  const module = {exports: {}};
  cache.set(absolute,module.exports);
  const code = ts.transpileModule(readFileSync(absolute,'utf8'), {
   compilerOptions: {target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}
  }).outputText;
  runInNewContext(code, {
   module, exports:module.exports, Buffer, setTimeout, clearTimeout,
   require: (name: string) => {
    if(name === 'vscode') { return vscode; }
    if(name.startsWith('./') && name.endsWith('.ts')) {
     const local = path.resolve(path.dirname(absolute),name);
     assert.ok(local.startsWith(path.resolve('src') + path.sep));
     return load(local);
    }
    assert.ok(Object.hasOwn(builtins,name), `Unexpected module: ${name}`);
    return builtins[name];
   }
  },{filename:absolute});
  return module.exports;
 }
 const {GdrSession,GdrCache} = load('src/session.ts') as {
  GdrSession: typeof Session; GdrCache: typeof Cache;
 };
 const {RtosViewsPanel} = load('src/panel.ts') as {RtosViewsPanel: typeof Panel};
 const directory = mkdtempSync(path.join(tmpdir(),'gdr-startup-'));
 const loader = path.join(directory,'loader.gdb');
 writeFileSync(path.join(directory,'gdr.py'),'# runtime stub');
 writeFileSync(`${loader}.result`,'present');
 let reply: unknown = {schema_version:2,ok:true,data:{candidates:[],resolved:null}};
 let thrown: Error | null = null;
 const session = new GdrSession({id:'s1',name:'firmware',type:'cortex-debug',configuration:{gdrLoaderPath:loader},
  customRequest: async () => {if(thrown) {throw thrown;} return {result:JSON.stringify(reply)};}
 } as any, {directory,entryPoint:path.join(directory,'gdr.py'),manifest:null,overridden:true},
 settings,m=>logs.push(m),new GdrCache({get:()=>undefined,update:async()=>undefined} as any));
 const panel = new RtosViewsPanel({log:m=>logs.push(m),extensionUri:{} as any,
  costOptions:()=>({noHeap:false,noDetails:false}),recordDir:()=>'',fetchSnapshot:async()=>null,
  fetchHeapMap:async()=>null,layoutStore:{read:()=>null,write:()=>undefined}});
 panel.sessionStarted('s1','firmware');
 return {session,panel,alerts,logs,loader,directory,
  reply:(value:unknown)=>{reply=value;},throw:(error:Error)=>{thrown=error;},
  dispose:()=>{session.discardLaunchFiles();panel.sessionEnded('s1');rmSync(directory,{recursive:true,force:true});}};
}

test('panel container is titled GDR without changing extension identity',()=>{
 const manifest = JSON.parse(readFileSync('package.json','utf8'));
 assert.equal(manifest.contributes.viewsContainers.panel[0].title,'GDR');
 assert.equal(manifest.name,'gdb-rtos-views');
});

test('no supported RTOS produces an actionable warning before webview resolution, deduplicated per session',async()=>{
 const h=host();
 try {
  assert.equal(await h.session.onFirstStop(1),false);
  assert.equal(h.session.failureLevel,'warning');
  assert.match(h.session.errorMessage!,/no supported RTOS.*gdr.rtos.*gdr.version/);
  h.panel.sessionFailed('s1',h.session.errorMessage!,h.session.failureLevel);
  h.panel.sessionFailed('s1',h.session.errorMessage!,h.session.failureLevel);
  assert.equal(h.alerts.length,1);
  assert.equal(h.alerts[0].level,'warning');
  assert.match(h.alerts[0].message,/GDR: panel startup failed for "firmware"/);
 } finally {h.dispose();}
});

test('GDB evaluate failure retains its concrete cause in startup error notification',async()=>{
 const h=host();
 try {
  h.throw(new Error('Undefined command: gdr_json; runtime import failed'));
  assert.equal(await h.session.onFirstStop(1),false);
  assert.equal(h.session.failureLevel,'error');
  assert.match(h.session.errorMessage!,/runtime import failed/);
  h.panel.sessionFailed('s1',h.session.errorMessage!,h.session.failureLevel);
  assert.equal(h.alerts[0].level,'error');
  assert.match(h.alerts[0].message,/runtime import failed/);
 } finally {h.dispose();}
});

test('missing runtime path reports the actual entry point and a repair action',async()=>{
 const h=host();
 try {
  rmSync(path.join(h.directory,'gdr.py'));
  assert.equal(await h.session.onFirstStop(1),false);
  assert.equal(h.session.failureLevel,'error');
  assert.match(h.session.errorMessage!,/gdr.py.*gdr.runtimePath/);
 } finally {h.dispose();}
});

test('Python missing is reported without a panel or stopped frame',async()=>{
 const h=host();
 try {
  writeFileSync(`${h.loader}.result`,'absent');
  assert.equal(await h.session.watchLaunchPython(),false);
  h.panel.sessionFailed('s1',h.session.errorMessage!,h.session.failureLevel);
  assert.match(h.alerts[0].message,/Python/);
  assert.equal(h.alerts[0].level,'error');
 } finally {h.dispose();}
});

test('missing version and cancelled selection explain the required user action',async()=>{
 for (const candidates of [[{rtos:'freertos',version:null}], [{rtos:'freertos',version:'11.2.0'}]]) {
  const h=host();
  try {
   h.reply({schema_version:2,ok:true,data:{candidates,resolved:null}});
   assert.equal(await h.session.onFirstStop(1),false);
   assert.equal(h.session.failureLevel,'warning');
   assert.match(h.session.errorMessage!,/gdr.rtos and gdr.version/);
   assert.match(h.session.errorMessage!,candidates[0].version === null ? /no version/ : /cancelled/);
  } finally {h.dispose();}
 }
});

test('failed adapter initialization includes the verification failure',async()=>{
 const h=host({rtos:'freertos',version:'11.2.0',autoInit:true});
 try {
  h.reply({schema_version:2,ok:false,error:{code:'not_initialized',message:'unsupported kernel version'},data:null});
  assert.equal(await h.session.onFirstStop(1),false);
  assert.equal(h.session.failureLevel,'error');
  assert.match(h.session.errorMessage!,/initialization of freertos 11.2.0.*unsupported kernel version/);
 } finally {h.dispose();}
});

test('a successful retry clears the previous startup failure',async()=>{
 const h=host();
 try {
  assert.equal(await h.session.onFirstStop(1),false);
  h.reply({schema_version:2,ok:true,data:{candidates:[{rtos:'freertos',version:'11.2.0'}],resolved:{rtos:'freertos',version:'11.2.0'}}});
  assert.equal(await h.session.onFirstStop(2),true);
  assert.equal(h.session.errorMessage,null);
  assert.equal(h.session.initialized?.rtos,'freertos');
 } finally {h.dispose();}
});

test('late startup failures after termination do not notify',()=>{
 const h=host();
 try {
  h.panel.sessionEnded('s1');
  h.panel.sessionFailed('s1','late failure');
  assert.equal(h.alerts.length,0);
 } finally {h.dispose();}
});

test('failed probe envelope retains coded error rather than generic initialization failure',async()=>{
 const h=host();
 try {
  h.reply({schema_version:2,ok:false,error:{code:'access',message:'ELF symbols unreadable'},data:null});
  assert.equal(await h.session.onFirstStop(1),false);
  assert.equal(h.session.failureLevel,'error');
  assert.match(h.session.errorMessage!,/ELF symbols unreadable/);
 } finally {h.dispose();}
});
