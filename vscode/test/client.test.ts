/** Exercise the actual import-free webview script in a DOM, not a restated rule. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import ts from 'typescript';
import { renderApp } from '../src/webview/render.ts';
import type { PanelSnapshot } from '../src/snapshot.ts';

const client = ts.transpileModule(readFileSync('src/webview/client.ts', 'utf8'), {
	compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None }
}).outputText;

const snapshot: PanelSnapshot = {
	rtos: 'rtthread', rtosVersion: '4.0.5', gdrVersion: null, generation: 1, elapsedMs: null,
	collectedAt: 0, warnings: [], noHeap: false, noDetails: false,
	data: { tasks: { headers: ['Name', 'Prio', 'TCB'], rows: [['main', '10', '0x1000']], messages: [], elastic: [] },
		tables: {}, system: null, heap: null, capabilities: null }
};
const markup = () => renderApp({ sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot }], activeId: 's1' });

function setup() {
	const dom = new JSDOM('<div id="gdr-root"></div>', { runScripts: 'outside-only' });
	const messages: any[] = [];
	(dom.window as any).acquireVsCodeApi = () => ({ postMessage: (message: unknown) => messages.push(message) });
	dom.window.eval(client);
	const apply = (html: string) => {
		(dom.window as any).hostHtml = html;
		dom.window.eval('applyTrustedHostMarkup(document.getElementById("gdr-root"), hostHtml)');
	};
	apply(markup());
	return { dom, messages, apply, document: dom.window.document };
}

test('checkbox redraw preserves open groups, vertical position and keyed horizontal scroll', () => {
	const { dom, document, messages, apply } = setup();
	const tree = document.querySelector<HTMLDetailsElement>('details[data-disclosure="tables"]')!;
	const group = document.querySelector<HTMLDetailsElement>('details[data-disclosure="columns:task"]')!;
	assert.equal(group.open, false, 'column groups start collapsed');
	tree.open = true; group.open = true;
	document.querySelector<HTMLElement>('.gdr-body')!.scrollTop = 220;
	document.querySelector<HTMLElement>('.gdr-scroll')!.scrollLeft = 130;
	const box = document.querySelector<HTMLInputElement>('input[data-column="Prio"]')!;
	box.click();
	assert.equal(messages.at(-1).type, 'layout');
	assert.equal(tree.open, true);
	assert.equal(group.open, true);
	const parent = document.querySelector<HTMLInputElement>('input[data-table="task"]:not([data-column])')!;
	parent.click();
	assert.equal(group.open, true, 'parent checkbox does not toggle its disclosure');
	parent.click();
	apply(renderApp({ sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot }], activeId: 's1' }, { layout: messages.at(-1).layout }));
	assert.equal(document.querySelector('th[data-col="Prio"]'), null);
	assert.equal(document.querySelector<HTMLDetailsElement>('details[data-disclosure="tables"]')!.open, true);
	assert.equal(document.querySelector<HTMLDetailsElement>('details[data-disclosure="columns:task"]')!.open, true);
	assert.equal(document.querySelector<HTMLElement>('.gdr-body')!.scrollTop, 220);
	assert.equal(document.querySelector<HTMLElement>('.gdr-scroll')!.scrollLeft, 130);
	dom.window.close();
});

test('dragging a header reorders all row cells and posts column order without dragging its card', () => {
	const { dom, document, messages } = setup();
	const from = document.querySelector<HTMLElement>('th[data-col="TCB"]')!;
	const target = document.querySelector<HTMLElement>('th[data-col="Name"]')!;
	from.dispatchEvent(new dom.window.Event('dragstart', { bubbles: true }));
	assert.equal(document.querySelector('.gdr-card.gdr-dragging'), null);
	target.dispatchEvent(new dom.window.Event('drop', { bubbles: true, cancelable: true }));
	assert.deepEqual(Array.from(document.querySelectorAll('thead th')).map((node) => node.textContent), ['TCB', 'Name', 'Prio']);
	assert.deepEqual(Array.from(document.querySelectorAll('tbody td')).map((node) => node.textContent), ['0x1000', 'main', '10']);
	assert.deepEqual(Array.from(messages.at(-1).layout.tables.task.columnOrder), ['TCB', 'Name', 'Prio']);
	assert.deepEqual(Array.from(document.querySelectorAll('[data-tree-kind="task"] input[data-column]')).map((box) => (box as HTMLInputElement).dataset.column), ['TCB', 'Name', 'Prio']);
	dom.window.close();
});

test('build hint block appends new advice and resizes from its grip', () => {
	const { dom, document, apply } = setup();
	const withHints = (hints: string[]) => renderApp({ sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot: { ...snapshot, data: { ...snapshot.data, buildHints: hints } } }], activeId: 's1' });
	apply(withHints(['first']));
	const scroll = document.querySelector<HTMLElement>('.gdr-hints-scroll')!;
	const grip = document.querySelector<HTMLElement>('.gdr-hints-grip')!;
	assert.equal(scroll.style.height, '88px');
	grip.dispatchEvent(new dom.window.PointerEvent('pointerdown', { clientY: 100, pointerId: 1, bubbles: true, cancelable: true }));
	dom.window.dispatchEvent(new dom.window.PointerEvent('pointermove', { clientY: 180, pointerId: 1 }));
	dom.window.dispatchEvent(new dom.window.PointerEvent('pointerup', { clientY: 180, pointerId: 1 }));
	assert.equal(scroll.style.height, '168px');
	apply(withHints(['first', 'second']));
	assert.equal(document.querySelector<HTMLElement>('.gdr-hints-scroll')!.style.height, '168px');
	assert.equal(document.querySelector<HTMLElement>('.gdr-hints')!.classList.contains('gdr-hints-new'), true);
	dom.window.close();
});

test('resize grips have a full-height hit area at the header boundary, not an empty inline box', () => {
	const { dom, document } = setup();
	const style = document.createElement('style');
	// Only the gesture rules are needed; jsdom's CSS parser does not model
	// every VS Code theme colour function used by the rest of the stylesheet.
	const css = readFileSync('src/webview/style.css', 'utf8');
	style.textContent = css.slice(css.indexOf('.gdr-col-grip {'), css.indexOf('.gdr-tree {'));
	document.head.append(style);
	const grip = document.querySelector<HTMLElement>('.gdr-col-grip')!;
	const computed = dom.window.getComputedStyle(grip);
	assert.equal(computed.position, 'absolute');
	assert.equal(computed.top, '0px');
	assert.equal(computed.bottom, '0px');
	assert.equal(computed.right, '0px');
	assert.equal(computed.width, '8px');
	assert.equal(computed.cursor, 'col-resize');
	dom.window.close();
});

test('column boundary resizing persists exact tracks without starting a header drag', () => {
	const { dom, document, messages, apply } = setup();
	const header = document.querySelector<HTMLElement>('th[data-col="Name"]')!;
	const grip = header.querySelector<HTMLElement>('.gdr-col-grip')!;
	for (const node of document.querySelectorAll<HTMLElement>('thead th')) {
		node.getBoundingClientRect = () => ({ width: node === header ? 180 : 90 } as DOMRect);
	}
	const pointer = (type: string, x: number, button = 0) => new dom.window.MouseEvent(type, { clientX: x, button, bubbles: true, cancelable: true });
	grip.dispatchEvent(pointer('pointerdown', 180));
	assert.equal(header.draggable, false, 'native reorder is disabled during resizing');
	const drag = new dom.window.Event('dragstart', { bubbles: true, cancelable: true });
	header.dispatchEvent(drag);
	assert.equal(drag.defaultPrevented, true);
	assert.equal(header.classList.contains('gdr-column-dragging'), false);
	dom.window.dispatchEvent(pointer('pointermove', 110));
	assert.equal(header.style.width, '110px');
	assert.equal(header.closest('table')!.style.width, '290px');
	dom.window.dispatchEvent(pointer('pointerup', 100));
	assert.equal(header.draggable, true);
	assert.equal(messages.length, 1);
	const layout = messages[0].layout;
	assert.equal(layout.tables.task.widths.Name, 100, 'persist the requested width, not the old auto-layout rectangle');
	assert.equal(layout.tables.task.widths.Prio, 90);
	assert.equal(layout.tables.task.widths.TCB, 90);
	apply(renderApp({ sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot }], activeId: 's1' }, { layout }));
	const table = document.querySelector<HTMLTableElement>('[data-kind="task"] table')!;
	assert.equal(table.style.tableLayout, 'fixed');
	assert.equal(table.style.width, '280px');
	assert.deepEqual(Array.from(table.querySelectorAll<HTMLTableColElement>('col')).map((node) => node.style.width), ['100px', '90px', '90px']);
	const from = table.querySelector<HTMLElement>('th[data-col="TCB"]')!;
	const target = table.querySelector<HTMLElement>('th[data-col="Name"]')!;
	from.dispatchEvent(new dom.window.Event('dragstart', { bubbles: true }));
	target.dispatchEvent(new dom.window.Event('drop', { bubbles: true, cancelable: true }));
	assert.deepEqual(Array.from(table.querySelectorAll<HTMLTableColElement>('col')).map((node) => node.style.width), ['90px', '100px', '90px']);
	assert.equal(messages.at(-1).layout.tables.task.widths.Name, 100);
	dom.window.close();
});

test('resize clamps widths, ignores secondary buttons and cleans up on pointercancel', () => {
	const { dom, document, messages } = setup();
	const header = document.querySelector<HTMLElement>('th[data-col="Name"]')!;
	const grip = header.querySelector<HTMLElement>('.gdr-col-grip')!;
	header.getBoundingClientRect = () => ({ width: 180 } as DOMRect);
	const pointer = (type: string, x: number, button = 0) => new dom.window.MouseEvent(type, { clientX: x, button, bubbles: true, cancelable: true });
	grip.dispatchEvent(pointer('pointerdown', 180, 2));
	assert.equal(header.draggable, true);
	grip.dispatchEvent(pointer('pointerdown', 180));
	dom.window.dispatchEvent(pointer('pointermove', -900));
	assert.equal(header.style.width, '40px');
	dom.window.dispatchEvent(pointer('pointermove', 9000));
	assert.equal(header.style.width, '1600px');
	dom.window.dispatchEvent(pointer('pointercancel', 0));
	assert.equal(header.draggable, true);
	assert.equal(messages.length, 1);
	assert.equal(messages[0].layout.tables.task.widths.Name, 1600);
	dom.window.dispatchEvent(pointer('pointermove', 100));
	dom.window.dispatchEvent(pointer('pointerup', 100));
	assert.equal(header.style.width, '1600px');
	assert.equal(messages.length, 1, 'cancel removed the global gesture listeners');
	dom.window.close();
});

test('Display selection reorder keeps resized column tracks attached to their values', () => {
	const { dom, document, messages, apply } = setup();
	const layout = { order: [], hiddenTables: [], tables: { task: { kind: 'task', widths: { Name: 160, Prio: 60, TCB: 120 }, hiddenColumns: [] } } };
	apply(renderApp({ sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot }], activeId: 's1' }, { layout }));
	document.querySelector('[data-tree-kind="task"] [data-tree-column="TCB"]')!.dispatchEvent(new dom.window.Event('dragstart', { bubbles: true }));
	document.querySelector('[data-tree-kind="task"] [data-tree-column="Name"]')!.dispatchEvent(new dom.window.Event('drop', { bubbles: true, cancelable: true }));
	assert.deepEqual(Array.from(document.querySelectorAll<HTMLTableColElement>('col')).map((node) => node.style.width), ['120px', '160px', '60px']);
	assert.deepEqual(Array.from(document.querySelectorAll('tbody td')).map((node) => node.textContent), ['0x1000', 'main', '10']);
	assert.equal(messages.at(-1).layout.tables.task.widths.Name, 160);
	dom.window.close();
});

test('canvas paints six-pixel cells from serialized runs and hover names the painted tiny block', () => {
	const { dom, document, apply } = setup();
	let image: { data: Uint8ClampedArray };
	(dom.window.HTMLCanvasElement.prototype as any).getContext = () => ({
		createImageData: (width: number, height: number) => ({ data: new Uint8ClampedArray(width * height * 4) }),
		putImageData: (value: { data: Uint8ClampedArray }) => { image = value; }
	});
	apply('<main class="gdr-body" data-session="s1"><section class="gdr-card" data-kind="heap" draggable="true"><h2 class="gdr-card-title">Heap</h2><details data-disclosure="heap-map" open><summary>Heap map</summary><canvas data-heap-canvas="1"></canvas><div data-heap-tooltip="1" hidden></div></details></section></main>');
	(dom.window as any).testMap = { base: 0, end: 101, owners: true,
		blocks: [[0, 1, true, 'tiny'], [1, 100, false, null]],
		palette: { owners: { tiny: '#e15e3a' }, free: '#c8c8c8', unknown: '#4f8fd6' },
		grid: { width: 2, height: 1, scale: 6, runs: [[0, 1, 0], [1, 2, 1]] } };
	dom.window.dispatchEvent(new dom.window.MessageEvent('message', { origin: dom.window.origin, data: { type: 'heapMap', sessionId: 's1', map: (dom.window as any).testMap } }));
	const canvas = document.querySelector('canvas')!;
	assert.equal(canvas.width, 12);
	assert.equal(canvas.height, 6);
	assert.equal(canvas.style.width, '12px');
	assert.deepEqual(Array.from(image!.data.slice(0, 4)), [225, 94, 58, 255]);
	assert.deepEqual(Array.from(image!.data.slice(24, 28)), [200, 200, 200, 255]);
	canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 12, height: 6 } as DOMRect);
	canvas.dispatchEvent(new dom.window.MouseEvent('mousemove', { clientX: 5, clientY: 5 }));
	assert.equal(document.querySelector('[data-heap-tooltip]')!.textContent, 'owner:tiny, size: 1 B');
	canvas.dispatchEvent(new dom.window.MouseEvent('mousemove', { clientX: 6, clientY: 5 }));
	assert.equal(document.querySelector('[data-heap-tooltip]')!.textContent, 'owner:(free), size: 100 B');
	assert.equal(document.querySelector<HTMLElement>('[data-heap-tooltip]')!.hidden, false);
	assert.equal(canvas.hasAttribute('title'), false, 'native title must not create a second hover popup');
	canvas.dispatchEvent(new dom.window.MouseEvent('pointermove', { clientX: 3, clientY: 2 }));
	assert.equal(document.querySelector('[data-heap-tooltip]')!.textContent, 'owner:tiny, size: 1 B');
	canvas.dispatchEvent(new dom.window.Event('dragstart', { bubbles: true, cancelable: true }));
	assert.equal(document.querySelector('.gdr-card.gdr-dragging'), null);
	canvas.dispatchEvent(new dom.window.MouseEvent('pointerleave'));
	assert.equal(document.querySelector<HTMLElement>('[data-heap-tooltip]')!.hidden, true);
	assert.equal(canvas.hasAttribute('title'), false);
	const fold = document.querySelector<HTMLDetailsElement>('[data-disclosure="heap-map"]')!;
	assert.equal(fold.open, true, 'first drawing opens the map');
	fold.open = false;
	apply(document.querySelector('.gdr-body')!.outerHTML);
	assert.equal(document.querySelector<HTMLDetailsElement>('[data-disclosure="heap-map"]')!.open, false, 'redraw preserves collapsed map');
	dom.window.close();
});


test('dragging table titles synchronizes checkbox parents immediately', () => {
 const { dom, document, apply, messages } = setup();
 const data: PanelSnapshot = { ...snapshot, data: { ...snapshot.data, tables: { semaphore: { headers: ['Name'], rows: [['sem']], messages: [], elastic: [] } } } };
 apply(renderApp({ sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot: data }], activeId: 's1' }));
 const title = document.querySelector('[data-kind="semaphore"] .gdr-card-title')!;
 assert.equal(title.getAttribute('draggable'), 'true', 'the actual drag event target is the title, not its section');
 assert.equal(title.closest('section')!.hasAttribute('draggable'), false);
 const start = new dom.window.Event('dragstart', { bubbles: true, cancelable: true });
 title.dispatchEvent(start);
 assert.equal(start.defaultPrevented, false);
 assert.ok(title.classList.contains('gdr-dragging'));
 document.querySelector('[data-kind="task"]')!.dispatchEvent(new dom.window.Event('drop', { bubbles: true, cancelable: true }));
 const parents = Array.from(document.querySelectorAll<HTMLElement>('[data-tree-kind]')).map((node) => node.dataset.treeKind);
 assert.deepEqual(parents.slice(0, 4), ['semaphore', 'task', 'timer', 'heap']);
 assert.deepEqual(Array.from(messages.at(-1).layout.order), parents);
 assert.deepEqual(Array.from(document.querySelectorAll<HTMLElement>('section[data-kind]')).map((node) => node.dataset.kind), ['semaphore', 'task']);
 dom.window.close();
});


test('Display selection parent drag reorders cards and persists hidden-table positions', () => {
 const { dom, document, apply, messages } = setup();
 const data: PanelSnapshot = { ...snapshot, data: { ...snapshot.data, tables: { semaphore: { headers: ['Name'], rows: [['sem']], messages: [], elastic: [] } } } };
 const state = { sessions: [{ id: 's1', name: 'firmware', status: 'ok' as const, message: null, snapshot: data }], activeId: 's1' };
 const layout = { order: [], hiddenTables: ['task'], tables: {} };
 apply(renderApp(state, { layout }));
 const source = document.querySelector('[data-tree-kind="task"] summary')!;
 assert.equal(source.getAttribute('draggable'), 'true');
 source.dispatchEvent(new dom.window.Event('dragstart', { bubbles: true, cancelable: true }));
 document.querySelector('[data-tree-kind="mutex"]')!.dispatchEvent(new dom.window.MouseEvent('drop', { bubbles: true, cancelable: true }));
 const result = messages.at(-1).layout;
 assert.deepEqual(Array.from(result.order).slice(0, 5), ['timer', 'heap', 'semaphore', 'task', 'mutex']);
 assert.ok(result.hiddenTables.includes('task'));
 apply(renderApp(state, { layout: result }));
 assert.equal(document.querySelector('section[data-kind="task"]'), null);
 assert.deepEqual(Array.from(document.querySelectorAll<HTMLElement>('[data-tree-kind]')).slice(0, 5).map((node) => node.dataset.treeKind), ['timer', 'heap', 'semaphore', 'task', 'mutex']);
 dom.window.close();
});

test('Display selection child drag reorders table values, including hidden columns, across redraws', () => {
 const { dom, document, apply, messages } = setup();
 const state = { sessions: [{ id: 's1', name: 'firmware', status: 'ok' as const, message: null, snapshot }], activeId: 's1' };
 apply(renderApp(state, { layout: { order: [], hiddenTables: [], tables: { task: { kind: 'task', widths: {}, hiddenColumns: ['Prio'] } } } }));
 const source = document.querySelector('[data-tree-kind="task"] [data-tree-column="TCB"]')!;
 const target = document.querySelector('[data-tree-kind="task"] [data-tree-column="Name"]')!;
 source.dispatchEvent(new dom.window.Event('dragstart', { bubbles: true, cancelable: true }));
 target.dispatchEvent(new dom.window.MouseEvent('drop', { bubbles: true, cancelable: true }));
 const result = messages.at(-1).layout;
 assert.deepEqual(Array.from(result.tables.task.columnOrder), ['TCB', 'Name', 'Prio']);
 assert.deepEqual(Array.from(document.querySelectorAll('tbody td')).map((node) => node.textContent), ['0x1000', 'main']);
 apply(renderApp(state, { layout: result }));
 assert.deepEqual(Array.from(document.querySelectorAll('thead th')).map((node) => node.textContent), ['TCB', 'Name']);
 assert.deepEqual(Array.from(document.querySelectorAll('[data-tree-kind="task"] [data-tree-column]')).map((node) => (node as HTMLElement).dataset.treeColumn), ['TCB', 'Name', 'Prio']);
 const group = document.querySelector<HTMLDetailsElement>('[data-disclosure="columns:task"]')!;
 group.open = true;
 const box = document.querySelector<HTMLInputElement>('input[data-column="Name"]')!;
 box.click();
 assert.equal(group.open, true);
 assert.equal(box.checked, false);
 dom.window.close();
});

test('child drag cannot move a column into a different table', () => {
 const { dom, document, apply, messages } = setup();
 const data: PanelSnapshot = { ...snapshot, data: { ...snapshot.data, tables: { semaphore: { headers: ['Name'], rows: [['sem']], messages: [], elastic: [] } } } };
 apply(renderApp({ sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot: data }], activeId: 's1' }));
 document.querySelector('[data-tree-kind="task"] [data-tree-column="TCB"]')!.dispatchEvent(new dom.window.Event('dragstart', { bubbles: true }));
 document.querySelector('[data-tree-kind="semaphore"] [data-tree-column="Name"]')!.dispatchEvent(new dom.window.Event('drop', { bubbles: true, cancelable: true }));
 assert.equal(messages.length, 0);
 dom.window.close();
});
