import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import ts from 'typescript';
import { renderAddress, renderSourceLink, renderWaiters } from '../src/webview/render.ts';

const source = {file:'/src/tasks.c',name:'tasks.c',line:27};
const client = ts.transpileModule(readFileSync('src/webview/client.ts','utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.None}}).outputText;
function setup() {
 const dom = new JSDOM('<div id="gdr-root"></div>',{runScripts:'outside-only'});
 const messages: any[] = [];
 (dom.window as any).acquireVsCodeApi = () => ({postMessage:(m: unknown)=>messages.push(JSON.parse(JSON.stringify(m)))});
 dom.window.eval(client);
 const markup = '<main class="gdr-body" data-session="s1">' +
  renderSourceLink('<worker>',source,'0x1100') + renderAddress('<storage>','0x2200') +
  renderAddress('0x3300','0x3300') + renderWaiters('2@sender,receiver',[
   {name:'sender',address:0x1100,source}, {name:'receiver',address:0x4000,source:null}
  ],'queue|1|RecvWait') + '</main>';
 const apply = () => {
  (dom.window as any).markup=markup;
  dom.window.eval('applyTrustedHostMarkup(document.getElementById("gdr-root"),markup)');
 };
 apply();
 return {dom,messages,apply};
}

test('FreeRTOS waiter records open proven source or TCB memory with the owning session',()=>{
 const {dom,messages}=setup();
 const links=Array.from(dom.window.document.querySelectorAll<HTMLAnchorElement>('.gdr-waiter-menu a'));
 assert.equal(links.length,2);
 links[0].click();
 assert.deepEqual(messages.at(-1),{type:'openSource',file:source.file,name:source.name,line:27,fallbackAddress:'0x1100',sessionId:'s1'});
 links[1].click();
 assert.deepEqual(messages.at(-1),{type:'openMemory',address:'0x4000',sessionId:'s1'});
 dom.window.close();
});

test('raw, symbolized, source and waiter pointers have a selectable hover label without a button',()=>{
 const {dom,messages,apply}=setup();
 const doc=dom.window.document;
 const anchors=Array.from(doc.querySelectorAll<HTMLElement>('[data-pointer]'));
 assert.equal(anchors.length,5);
 for(const anchor of anchors) {
  anchor.dispatchEvent(new dom.window.MouseEvent('mouseover',{bubbles:true}));
  const label=doc.querySelector<HTMLElement>('.gdr-pointer-label')!;
  assert.equal(label.hidden,false);
  assert.equal(label.querySelector('code')?.textContent,anchor.dataset.pointer);
  assert.equal(label.querySelector('button'),null);
  const range=doc.createRange();
  range.selectNodeContents(label.querySelector('code')!);
  dom.window.getSelection()!.removeAllRanges();
  dom.window.getSelection()!.addRange(range);
  assert.equal(dom.window.getSelection()!.toString(),anchor.dataset.pointer);
  label.click();
  assert.equal(messages.length,0);
 }
 assert.equal(doc.querySelectorAll('.gdr-pointer-label').length,1);
 apply();
 assert.equal(doc.querySelector<HTMLElement>('.gdr-pointer-label')!.hidden,true);
 const anchor=doc.querySelector<HTMLElement>('[data-pointer]')!;
 anchor.dispatchEvent(new dom.window.FocusEvent('focusin',{bubbles:true}));
 assert.equal(doc.querySelector<HTMLElement>('.gdr-pointer-label')!.hidden,false);
 doc.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
 assert.equal(doc.querySelector<HTMLElement>('.gdr-pointer-label')!.hidden,true);
 dom.window.close();
});
