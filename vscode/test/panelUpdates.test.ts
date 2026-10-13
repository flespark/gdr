import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import ts from 'typescript';
import { parseSnapshot, type PanelSnapshot } from '../src/snapshot.ts';
import { changedKeys } from '../src/diff.ts';
import { renderApp } from '../src/webview/render.ts';

const source = { file: '/src/main.c', name: 'main.c', line: 9 };
function snapshot(pc: number = 0x1000, tick = '1'): PanelSnapshot {
 const data = parseSnapshot({
  tasks: { headers: ['Name', 'PC', 'Entry', 'Addr'], rows: [['a', '<worker>', '<entry>', '0x2000'], ['b', '<idle>', '<idle>', '0x3000']], messages: [], elastic: [],
   ids: [0x2000, 0x3000], value_rows: [['a', pc, 0x1010, 0x2000], ['b', 0x1020, 0x1020, 0x3000]],
   columns: ['text', 'pc', 'symbol', 'addr'].map((kind, i) => ({ key: ['Name','PC','Entry','Addr'][i], kind })), source_rows: [[null, source, source, null]] },
  tables: {
   timer: { headers: ['Name','Callback','Parameter'], rows: [['t','<cb>','<parameter>']], ids: [0x4000], messages: [], elastic: [], columns: [{key:'Name'}, {key:'Callback',kind:'symbol'}, {key:'Parameter',kind:'symbol'}], value_rows: [['t',0x1000,0x5000]], source_rows: [[null, source, null]] },
   mailbox: { headers: ['Name','RecvWait','Slot'], rows: [['m','2@a,b','Slot[0]: 0x6000']], ids: [0x7000], messages: [], elastic: [], columns: [{key:'Name'}, {key:'RecvWait',kind:'waiters'}, {key:'Slot',kind:'addresses'}], value_rows: [['m',[{name:'a',source,address:0x1010},'b'],[{text:'Slot[0]: 0x6000',address:0x6000}]]] }
  },
  system: {pairs:[['Tick count',tick]]}, heap: {pairs:[['UsedSize',tick]],messages:[],sections:[{title:'checks',rows:[{key:'Chain',value:tick,severity:'ok'}]}]}
 })!;
 return { rtos:'rtthread',rtosVersion:'4.0.5',gdrVersion:null,generation:1,elapsedMs:1,collectedAt:0,warnings:[],noHeap:false,noDetails:false,data };
}
function html(data = snapshot(), changed?: Set<string>, id = 's1') {
 return renderApp({sessions:[{id,name:'GDB',status:'ok',message:null,snapshot:data}],activeId:id}, changed ? {changed} : {});
}
const client = ts.transpileModule(readFileSync('src/webview/client.ts','utf8'), {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.None}}).outputText;
function setup() {
 const dom = new JSDOM('<div id="gdr-root"></div>',{runScripts:'outside-only'});
 const messages: any[] = [];
 (dom.window as any).acquireVsCodeApi = () => ({postMessage:(m: unknown) => messages.push(JSON.parse(JSON.stringify(m)))});
 dom.window.eval(client);
 const apply = (markup: string) => { (dom.window as any).hostHtml = markup; dom.window.eval('applyTrustedHostMarkup(document.getElementById("gdr-root"),hostHtml)'); };
 apply(html());
 return {dom,document:dom.window.document,messages,apply};
}
test('numeric runtime row IDs remain distinct and raw PC changes highlight symbol links', () => {
 const before = snapshot(); const after = snapshot(0x1004,'2');
 assert.deepEqual(after.data.tasks?.ids,['8192','12288']);
 const changed = changedKeys(before, after);
 assert.ok(changed.has('task|8192|PC'));
 assert.ok(!changed.has('task|12288|PC'));
 const dom = new JSDOM(html(after,changed)); const doc = dom.window.document;
 assert.ok(doc.querySelector('[data-id="8192"] td.gdr-changed a.gdr-source'));
 assert.ok(doc.querySelector('.gdr-system-table td.gdr-changed'));
 assert.ok(doc.querySelector('.gdr-heap-table td.gdr-changed'));
 assert.ok(doc.querySelector('.gdr-verdict-table td.gdr-changed'));
 const header = doc.querySelector('.gdr-session-header')!;
 assert.deepEqual(Array.from(header.children).map(n=>n.className), ['gdr-identity','gdr-tabs','gdr-meta']);
 dom.window.close();
});
test('callback source, unresolved parameter and mailbox slot route to their proper destinations', () => {
 const {dom,document:doc,messages} = setup();
 (doc.querySelector('[data-kind="timer"] .gdr-source') as HTMLElement).click();
 assert.deepEqual(messages.at(-1),{type:'openSource',file:source.file,name:source.name,line:9,fallbackAddress:'0x1000',sessionId:'s1'});
 (doc.querySelector('[data-address="0x5000"]') as HTMLElement).click();
 assert.equal(messages.at(-1).address,'0x5000');
 (doc.querySelector('[data-address="0x6000"]') as HTMLElement).click();
 assert.deepEqual(messages.at(-1),{type:'openMemory',address:'0x6000',sessionId:'s1'});
 (doc.querySelector('.gdr-tab') as HTMLElement).click();
 assert.deepEqual(messages.at(-1),{type:'selectSession',sessionId:'s1'});
 dom.window.close();
});
test('waiters show counts, preserve expansion on refresh/reorder, and do not leak across sessions', () => {
 const {dom,document:doc,apply,messages} = setup();
 let fold = doc.querySelector<HTMLDetailsElement>('.gdr-waiter-fold')!;
 assert.equal(fold.querySelector('summary')?.textContent,'2');
 assert.equal(fold.querySelectorAll('li').length,2);
 fold.open = true;
 apply(html(snapshot(0x1004)));
 fold = doc.querySelector<HTMLDetailsElement>('.gdr-waiter-fold')!;
 assert.ok(fold.open);
 assert.equal(fold.querySelector<HTMLElement>('ul')?.style.position ?? '', ''); // position is stylesheet-owned
 assert.ok(fold.querySelector<HTMLElement>('ul')?.style.top);
 (fold.querySelector('.gdr-source') as HTMLElement).click();
 assert.equal(messages.at(-1).type,'openSource');
 apply(html(snapshot(),undefined,'s2'));
 assert.equal(doc.querySelector<HTMLDetailsElement>('.gdr-waiter-fold')?.open,false);
 dom.window.close();
});
test('narrow tables stay content-sized with continuous uncollapsed cell borders', () => {
 const css = readFileSync('src/webview/style.css','utf8');
 assert.match(css, /\.gdr-table th:last-child,\s*\.gdr-table td:last-child\s*\{\s*border-right: 1px solid/);
 const dom = new JSDOM(`<style>${css}</style>${html()}`);
 for (const table of dom.window.document.querySelectorAll('.gdr-table')) {
  const style = dom.window.getComputedStyle(table);
  assert.equal(style.width, 'max-content');
  assert.equal(style.borderCollapse, 'separate');
  assert.match(style.borderSpacing, /^0(?:px)?(?: 0(?:px)?)?$/);
 }
 for (const container of dom.window.document.querySelectorAll('.gdr-card, .gdr-scroll')) {
  const style = dom.window.getComputedStyle(container);
  assert.equal(style.width, 'fit-content');
  assert.equal(style.maxWidth, '100%');
 }
 dom.window.close();
});
test('link highlight and waiter overlay CSS do not inherit the normal link colour or clip in tables', () => {
 const css = readFileSync('src/webview/style.css','utf8');
 assert.match(css,/\.gdr-changed,\s*\.gdr-changed a\s*\{/);
 assert.match(css,/\.gdr-waiter-menu\s*\{\s*position: fixed;/);
});
