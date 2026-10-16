/** Exercise the heap viewer wiring in a DOM: legend, endpoint labels, hover and pan. */
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

const map = {
	algorithm: 'small_mem', base: 0, end: 700, owners: true, truncated: false, messages: [],
	blocks: [[0, 64, true, 'main'], [64, 636, false, null]],
	palette: { owners: { main: '#e15e3a' }, free: '#c8c8c8', unknown: '#4f8fd6' },
	grid: {
		width: 2, height: 2, scale: 6, runs: [[0, 3, 0], [3, 4, 1]],
		folds: [{ row: 1, height: 2, bytes: 500, kind: 'free', start: 64, end: 564 }]
	}
};

const snapshot: PanelSnapshot = {
	rtos: 'rtthread', rtosVersion: '4.0.5', gdrVersion: null, generation: 1, elapsedMs: null,
	collectedAt: 0, warnings: [], noHeap: false, noDetails: false,
	data: {
		tasks: null, tables: {}, system: null, capabilities: null,
		heap: { pairs: [], messages: [], sections: [] }
	}
};

/** Minimal 2d context: pixels are not under test, sizes and DOM wiring are. */
function stubCanvas(dom: JSDOM): void {
	const context = {
		canvas: null,
		fillStyle: '#000000',
		strokeStyle: '#000000',
		font: '',
		textBaseline: 'alphabetic',
		createImageData: (width: number, height: number) => ({ width, height, data: new Uint8ClampedArray(width * height * 4) }),
		putImageData: () => {},
		fillRect: () => {},
		fillText: () => {},
		measureText: () => ({ width: 10 })
	};
	(dom.window.HTMLCanvasElement.prototype as unknown as { getContext: unknown }).getContext =
		function (this: unknown, kind: string): unknown {
			if (kind !== '2d') { return null; }
			return { ...context, canvas: this };
		};
}

function setup() {
	const dom = new JSDOM('<div id="gdr-root"></div>', { runScripts: 'outside-only' });
	const messages: unknown[] = [];
	(dom.window as any).acquireVsCodeApi = () => ({ postMessage: (message: unknown) => messages.push(message) });
	stubCanvas(dom);
	dom.window.eval(client);
	const html = renderApp({
		sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot }],
		activeId: 's1'
	}, { heapCanvas: true });
	(dom.window as any).hostHtml = html;
	dom.window.eval('applyTrustedHostMarkup(document.getElementById("gdr-root"), hostHtml)');
	dom.window.dispatchEvent(new dom.window.MessageEvent('message', {
		origin: dom.window.origin,
		data: { type: 'heapMap', sessionId: 's1', map }
	}));
	const document = dom.window.document;
	return {
		dom, document, messages,
		canvas: document.querySelector<HTMLCanvasElement>('canvas[data-heap-canvas]')!,
		viewport: document.querySelector<HTMLElement>('.gdr-heap-map')!,
		legend: document.querySelector<HTMLElement>('[data-heap-legend]')!,
		tooltip: document.querySelector<HTMLElement>('[data-heap-tooltip]')!
	};
}

function hoverAt(dom: JSDOM, canvas: HTMLCanvasElement, clientX: number, clientY: number): void {
	canvas.getBoundingClientRect = () => ({ width: 12, height: 18, left: 0, top: 0, right: 12, bottom: 18, x: 0, y: 0, toJSON: () => ({}) });
	canvas.dispatchEvent(new dom.window.MouseEvent('mousemove', { bubbles: true, clientX, clientY }));
}

test('the viewer paints bitmap, endpoint labels and a one-column ranking', () => {
	const { dom, canvas, legend, document } = setup();
	assert.equal(canvas.width, 12);
	assert.equal(canvas.height, 18, 'the folded row renders bands, not bands plus cells');
	assert.equal(document.querySelector('[data-heap-addr-start]')!.textContent, 'start: 0x0');
	assert.equal(document.querySelector('[data-heap-addr-end]')!.textContent, 'end: 0x2bc');
	const rows = Array.from(legend.querySelectorAll('.gdr-heap-legend-row'));
	assert.deepEqual(rows.map((row) => row.textContent), [
		'main: 64B, 0x0-0x40',
		'(free): 636B, 0x40-0x2bc'
	]);
	assert.equal(legend.querySelectorAll('.gdr-heap-legend-swatch').length, 2);
	assert.equal(legend.querySelectorAll('.gdr-heap-legend-col').length, 1);
	dom.window.close();
});

test('hover names the block and the divider with their address ranges', () => {
	const { dom, canvas, tooltip } = setup();
	canvas.getBoundingClientRect = () => ({ width: 12, height: 18, left: 0, top: 0, right: 12, bottom: 18, x: 0, y: 0, toJSON: () => ({}) });
	canvas.dispatchEvent(new dom.window.MouseEvent('mousemove', { bubbles: true, clientX: 3, clientY: 3 }));
	assert.equal(tooltip.hidden, false);
	assert.equal(tooltip.textContent, 'owner:main, size: 64 B\nstart: 0x0, end: 0x40');
	// Reason: the two-unit band replaces grid row 1.
	canvas.dispatchEvent(new dom.window.MouseEvent('mousemove', { bubbles: true, clientX: 3, clientY: 8 }));
	assert.equal(tooltip.textContent, 'folded: 500 B (free)\nstart: 0x40, end: 0x234');
	// Reason: the free block's only cell sits in the folded row, so its
	// pixels name the band, not the block.
	canvas.dispatchEvent(new dom.window.MouseEvent('mousemove', { bubbles: true, clientX: 9, clientY: 15 }));
	assert.equal(tooltip.textContent, 'folded: 500 B (free)\nstart: 0x40, end: 0x234');
	dom.window.close();
});

test('the label pins per block beside the cell instead of trailing the cursor', () => {
	const { dom, canvas, tooltip } = setup();
	hoverAt(dom, canvas, 3, 3);
	assert.equal(tooltip.hidden, false);
	// Reason: the first cell is six pixels wide, so the label docks right of it.
	assert.equal(tooltip.style.left, '14px');
	hoverAt(dom, canvas, 4, 4);
	assert.equal(tooltip.textContent, 'owner:main, size: 64 B\nstart: 0x0, end: 0x40');
	assert.equal(tooltip.style.left, '14px', 'same block, no reposition');
	hoverAt(dom, canvas, 9, 15);
	assert.equal(tooltip.textContent, 'folded: 500 B (free)\nstart: 0x40, end: 0x234');
	assert.equal(tooltip.style.left, '20px', 'the band spans the row, so the label docks right of the canvas');
	dom.window.close();
});

function sizedGrid(width: number, rowBytes: number): Record<string, unknown> {
	return {
		type: 'heapGrid', sessionId: 's1', rowBytes,
		grid: { width, height: 1, scale: 6, runs: [[0, width - 1, 0], [width - 1, width, 1]], folds: [{ row: 1, height: 1, bytes: 636, kind: 'free', start: 64, end: 700 }] }
	};
}

function sendHeapGrid(dom: JSDOM, payload: Record<string, unknown>): void {
	dom.window.dispatchEvent(new dom.window.MessageEvent('message', { origin: dom.window.origin, data: payload }));
}

test('dragging the right-edge grip resizes the bitmap with live folded bytes', () => {
	const { dom, document, messages, canvas } = setup();
	const grip = document.querySelector<HTMLElement>('[data-heap-grip]')!;
	const label = document.querySelector<HTMLElement>('[data-heap-resize]')!;
	assert.equal(label.hidden, true);
	// Reason: the tidied envelope is two columns wide, below any minimum;
	// start from a realistic relaid grid so clamps stay out of the way.
	sendHeapGrid(dom, sizedGrid(40, 16));
	assert.equal(canvas.width, 240);
	grip.dispatchEvent(new dom.window.PointerEvent('pointerdown', { bubbles: true, button: 0, clientX: 100, clientY: 50 }));
	dom.window.dispatchEvent(new dom.window.PointerEvent('pointermove', { bubbles: true, clientX: 112, clientY: 50 }));
	const posted = messages.at(-1) as { type?: unknown; columns?: unknown; sessionId?: unknown };
	assert.equal(posted?.type, 'resizeHeap');
	assert.equal(posted?.columns, 42, 'twelve pixels at six per cell widen two columns');
	assert.equal(posted?.sessionId, 's1');
	// Reason: the host answers with a relaid grid; the view paints it and
	// the label names the folded bytes the new width leaves collapsed.
	sendHeapGrid(dom, sizedGrid(42, 64));
	assert.equal(canvas.width, 252);
	assert.equal(label.hidden, false);
	assert.equal(label.textContent, 'row bytes: 64 B');
	dom.window.dispatchEvent(new dom.window.PointerEvent('pointerup', { bubbles: true }));
	assert.equal(label.hidden, true, 'release hides the live label');
	assert.equal(canvas.width, 252, 'the resized layout stays as view state');
	dom.window.close();
});

test('double-clicking the grip resets to the envelope grid', () => {
	const { dom, document, messages, canvas } = setup();
	const grip = document.querySelector<HTMLElement>('[data-heap-grip]')!;
	sendHeapGrid(dom, sizedGrid(40, 16));
	assert.equal(canvas.width, 240);
	grip.dispatchEvent(new dom.window.MouseEvent('dblclick', { bubbles: true }));
	const posted = messages.at(-1) as { type?: unknown; sessionId?: unknown };
	assert.equal(posted?.type, 'resetHeapSize');
	assert.equal(posted?.sessionId, 's1');
	assert.equal(canvas.width, 12, 'the envelope grid draws again immediately');
	dom.window.close();
});

test('expanding the empty heap map requests the draw', () => {
	const dom = new JSDOM('<div id="gdr-root"></div>', { runScripts: 'outside-only' });
	const messages: unknown[] = [];
	(dom.window as any).acquireVsCodeApi = () => ({ postMessage: (message: unknown) => messages.push(message) });
	stubCanvas(dom);
	dom.window.eval(client);
	const html = renderApp({
		sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot }],
		activeId: 's1'
	});
	(dom.window as any).hostHtml = html;
	dom.window.eval('applyTrustedHostMarkup(document.getElementById("gdr-root"), hostHtml)');
	const document = dom.window.document;
	const fold = document.querySelector<HTMLDetailsElement>('details[data-disclosure="heap-map"]')!;
	assert.equal(fold.open, false, 'unrequested starts collapsed');
	assert.equal(document.querySelector('canvas[data-heap-canvas]'), null);
	fold.open = true;
	fold.dispatchEvent(new dom.window.Event('toggle', { bubbles: true }));
	const posted = messages.at(-1) as { type?: unknown; sessionId?: unknown };
	assert.equal(posted?.type, 'heapDump');
	assert.equal(posted?.sessionId, 's1');
	dom.window.close();
});

test('the address span links open the memory view with the session', () => {
	const { dom, canvas, tooltip, messages } = setup();
	hoverAt(dom, canvas, 3, 3);
	const links = Array.from(tooltip.querySelectorAll<HTMLButtonElement>('[data-address]'));
	assert.deepEqual(links.map((link) => link.dataset.address), ['0x0', '0x40']);
	links[1]!.click();
	const posted = messages.at(-1) as { type?: unknown; address?: unknown; sessionId?: unknown };
	assert.equal(posted?.type, 'openMemory');
	assert.equal(posted?.address, '0x40');
	assert.equal(posted?.sessionId, 's1');
	dom.window.close();
});

test('travelling between canvas and label keeps the pinned label', () => {
	const { dom, canvas, tooltip, document } = setup();
	hoverAt(dom, canvas, 3, 3);
	assert.equal(tooltip.hidden, false);
	canvas.dispatchEvent(new dom.window.MouseEvent('mouseleave', { bubbles: true, relatedTarget: tooltip }));
	assert.equal(tooltip.hidden, false, 'moving onto the label keeps it');
	assert.equal(tooltip.textContent, 'owner:main, size: 64 B\nstart: 0x0, end: 0x40');
	tooltip.dispatchEvent(new dom.window.MouseEvent('mouseleave', { bubbles: true, relatedTarget: document.body }));
	assert.equal(tooltip.hidden, true, 'leaving both hides it');
	hoverAt(dom, canvas, 3, 3);
	tooltip.dispatchEvent(new dom.window.MouseEvent('mouseleave', { bubbles: true, relatedTarget: canvas }));
	assert.equal(tooltip.hidden, false, 'moving back to the canvas keeps it until refresh');
	dom.window.close();
});

test('dragging the corner grip scales the bitmap proportionally', () => {
	const { dom, document, canvas } = setup();
	const corner = document.querySelector<HTMLElement>('[data-heap-corner]')!;
	canvas.getBoundingClientRect = () => ({ width: 12, height: 18, left: 0, top: 0, right: 12, bottom: 18, x: 0, y: 0, toJSON: () => ({}) });
	assert.equal(canvas.style.width, '12px');
	corner.dispatchEvent(new dom.window.PointerEvent('pointerdown', { bubbles: true, button: 0, clientX: 12, clientY: 18 }));
	dom.window.dispatchEvent(new dom.window.PointerEvent('pointermove', { bubbles: true, clientX: 24, clientY: 36 }));
	assert.equal(canvas.style.width, '24px');
	assert.equal(canvas.style.height, '36px');
	assert.equal(canvas.width, 12, 'scaling never repaints the backing store');
	dom.window.dispatchEvent(new dom.window.PointerEvent('pointermove', { bubbles: true, clientX: 1200, clientY: 2400 }));
	assert.equal(canvas.style.width, '48px', 'zoom clamps at four times');
	assert.equal(canvas.style.height, '72px');
	dom.window.dispatchEvent(new dom.window.PointerEvent('pointerup', { bubbles: true }));
	corner.dispatchEvent(new dom.window.MouseEvent('dblclick', { bubbles: true }));
	assert.equal(canvas.style.width, '12px');
	assert.equal(canvas.style.height, '18px');
	dom.window.close();
});

test('dragging pans the viewport and suppresses the hover until release', () => {
	const { dom, canvas, viewport, tooltip } = setup();
	canvas.getBoundingClientRect = () => ({ width: 12, height: 18, left: 0, top: 0, right: 12, bottom: 18, x: 0, y: 0, toJSON: () => ({}) });
	viewport.dispatchEvent(new dom.window.PointerEvent('pointerdown', { bubbles: true, button: 0, clientX: 0, clientY: 0 }));
	viewport.dispatchEvent(new dom.window.PointerEvent('pointermove', { bubbles: true, clientX: 40, clientY: 40 }));
	assert.ok(viewport.classList.contains('gdr-heap-panning'));
	canvas.dispatchEvent(new dom.window.MouseEvent('mousemove', { bubbles: true, clientX: 3, clientY: 3 }));
	assert.equal(tooltip.hidden, true);
	viewport.dispatchEvent(new dom.window.PointerEvent('pointerup', { bubbles: true }));
	assert.ok(!viewport.classList.contains('gdr-heap-panning'));
	dom.window.close();
});
