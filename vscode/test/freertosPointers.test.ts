import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import ts from 'typescript';
import { parseSnapshot, type PanelSnapshot } from '../src/snapshot.ts';
import { renderApp } from '../src/webview/render.ts';

test('FreeRTOS symbolized queue pointers and items open raw memory addresses, not symbol expressions', () => {
 const pointers = ['Head', 'Tail', 'WriteTo', 'ReadFrom', 'Set'];
 const addresses = [0x2100, 0x2200, 0x2104, 0x2100, 0x3000];
 const data = parseSnapshot({tables: {queue: {
  headers: ['Name', ...pointers, 'Item'],
  rows: [['q', ...pointers.map((_, i) => `<storage+${i}>`), 'Item[0]: 0x2104']],
  ids: [0x2000], messages: [], elastic: [],
  columns: [{key:'Name'}, ...pointers.map(key => ({key, kind:'symbol'})), {key:'Item', kind:'addresses'}],
  value_rows: [['q', ...addresses, [{text:'Item[0]: 0x2104', address:0x2104}]]]
 }}})!;
 const snapshot: PanelSnapshot = {rtos:'freertos',rtosVersion:'11.2.0',gdrVersion:null,generation:1,elapsedMs:0,collectedAt:0,warnings:[],noHeap:true,noDetails:false,data};
 const dom = new JSDOM('<div id="gdr-root"></div>', {runScripts:'outside-only'});
 const messages: any[] = [];
 (dom.window as any).acquireVsCodeApi = () => ({postMessage:(message: unknown) => messages.push(JSON.parse(JSON.stringify(message)))});
 dom.window.eval(ts.transpileModule(readFileSync('src/webview/client.ts','utf8'), {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.None}}).outputText);
 (dom.window as any).markup = renderApp({sessions:[{id:'s1',name:'FRT',status:'ok',message:null,snapshot}],activeId:'s1'});
 dom.window.eval('applyTrustedHostMarkup(document.getElementById("gdr-root"),markup)');
 const links = [...dom.window.document.querySelectorAll<HTMLAnchorElement>('[data-kind="queue"] tbody a[data-address]')];
 assert.equal(links.length, 6);
 links.forEach((link, i) => {
  link.click();
  assert.deepEqual(messages.at(-1), {type:'openMemory',address:`0x${(addresses[i] ?? 0x2104).toString(16)}`,sessionId:'s1'});
 });
 dom.window.close();
});
