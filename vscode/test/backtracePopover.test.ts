/** Tests for Tasks PC backtrace popover, knob-detach floating, close button, and bottom height grip. */
import assert from 'node:assert/strict';
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import { readFileSync } from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';
import { JSDOM } from 'jsdom';
import ts from 'typescript';

import type { RtosViewsPanel as Panel } from '../src/panel.ts';
import type { GdrEnvelope } from '../src/transport.ts';
import { parseBacktrace, parseSnapshot, type BacktraceData, type PanelSnapshot } from '../src/snapshot.ts';
import { decodeWebviewMessage } from '../src/webview/messages.ts';
import {
	backtraceStatus,
	renderApp,
	renderBacktraceBody,
	type BacktraceEntry
} from '../src/webview/render.ts';

const client = ts.transpileModule(readFileSync('src/webview/client.ts', 'utf8'), {
	compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None }
}).outputText;

const css = readFileSync('src/webview/style.css', 'utf8');
const taskSource = { file: '/src/main.c', name: 'profiles/main.c', line: 488 };
const irqSource = { file: '/src/port.c', name: 'portable/port.c', line: 102 };

function makeSnapshot(generation = 1): PanelSnapshot {
	const data = parseSnapshot({
		tasks: {
			headers: ['Name', 'State', 'PC', 'Addr'],
			rows: [
				['gdr_recm *', 'Running', '<gdr_recursive_hold+24>', '0x28002cc0'],
				['gdr_fpu', 'Blocked', '<vPortYield+12>', '0x28000f80'],
				['peer', 'Running', 'N/A', '0x28003000']
			],
			ids: [0x28002cc0, 0x28000f80, 0x28003000],
			value_rows: [
				['gdr_recm *', 'Running', 0x10000488, 0x28002cc0],
				['gdr_fpu', 'Blocked', 0x100028bc, 0x28000f80],
				['peer', 'Running', null, 0x28003000]
			],
			source_rows: [
				[null, null, taskSource, null],
				[null, null, null, null],
				[null, null, null, null]
			],
			columns: [
				{ key: 'Name', kind: 'text' },
				{ key: 'State', kind: 'text' },
				{ key: 'PC', kind: 'pc' },
				{ key: 'Addr', kind: 'addr' }
			],
			messages: [],
			elastic: []
		},
		tables: {
			semaphore: {
				headers: ['Name', 'Waiters'],
				rows: [['gdr_semaphore', '1@gdr_semw']],
				ids: [0x28000440],
				value_rows: [['gdr_semaphore', [{ name: 'gdr_semw', address: 0x28001940, source: taskSource }]]],
				columns: [
					{ key: 'Name', kind: 'text' },
					{ key: 'Waiters', kind: 'waiters' }
				],
				messages: [],
				elastic: []
			}
		},
		system: null,
		heap: null,
		capabilities: null
	})!;
	return {
		rtos: 'freertos',
		rtosVersion: '11.1.0',
		gdrVersion: '2026.4',
		generation,
		elapsedMs: 1,
		collectedAt: 0,
		warnings: [],
		noHeap: false,
		noDetails: false,
		data
	};
}

function makeNestedBacktrace(): BacktraceData {
	return parseBacktrace({
		task: 'gdr_recm',
		address: 0x28002cc0,
		architecture: 'armv8-m.mainline',
		abi: 'aapcs32-vfp',
		method: 'exception',
		source: 'current',
		complete: true,
		stop_reason: 'task-exit',
		truncated: false,
		max_depth: 32,
		bounds: 'recorded',
		frames: [
			{ index: 0, pc: 0x10000100, symbol: 'SysTick_Handler+8', source: irqSource, method: 'seed' },
			{ index: 1, pc: 0x10000488, symbol: 'gdr_recursive_hold+24', source: taskSource, method: 'exception' },
			{ index: 2, pc: 0x10002868, symbol: 'prvTaskExitError', source: null, method: 'cfi' }
		],
		messages: []
	})!;
}

function setup(backtraces?: Map<string, BacktraceEntry>, generation = 1) {
	const dom = new JSDOM('<div id="gdr-root"></div>', { runScripts: 'outside-only' });
	const messages: any[] = [];
	(dom.window as any).acquireVsCodeApi = () => ({
		postMessage: (message: unknown) => messages.push(JSON.parse(JSON.stringify(message)))
	});
	dom.window.eval(client);
	const apply = (gen = generation, cached = backtraces) => {
		const html = renderApp(
			{
				sessions: [{ id: 's1', name: 'QEMU', status: 'ok', message: null, snapshot: makeSnapshot(gen) }],
				activeId: 's1'
			},
			cached ? { backtraces: cached } : {}
		);
		(dom.window as any).hostHtml = html;
		dom.window.eval('applyTrustedHostMarkup(document.getElementById("gdr-root"), hostHtml)');
	};
	apply();
	return { dom, document: dom.window.document, messages, apply };
}

test('parseBacktrace and decodeWebviewMessage validate backtrace contracts', () => {
	const parsed = makeNestedBacktrace();
	assert.equal(parsed.task, 'gdr_recm');
	assert.equal(parsed.frames.length, 3);
	assert.equal(parsed.frames[1].method, 'exception');
	assert.equal(parseBacktrace({ notATask: true }), null);

	assert.deepEqual(
		decodeWebviewMessage({ type: 'backtrace', target: '0x28002cc0', key: 'task|671100096|PC', sessionId: 's1' }),
		{ type: 'backtrace', target: '0x28002cc0', key: 'task|671100096|PC', sessionId: 's1' }
	);
	assert.equal(decodeWebviewMessage({ type: 'backtrace', target: '  ', key: 'k' }), null);
	assert.equal(decodeWebviewMessage({ type: 'backtrace', target: '0x10', key: '' }), null);
});

test('Tasks PC cells render clean backtrace popovers without method tags, source suffix, report footers, or drag knob', () => {
	assert.match(css, /\.gdr-popover-list\s*\{[^}]*list-style:\s*none;[^}]*padding:\s*0 0 1\.6em;/);
	assert.match(css, /\.gdr-popover-grip-overflow\s*\{/);
	assert.match(css, /\.gdr-popover-title\s*\{[^}]*display:\s*inline-flex;/);
	assert.match(css, /\.gdr-popover-grip\s*\{[^}]*height:\s*11px;/);
	const report = makeNestedBacktrace();
	report.messages.push("task '0x28002cc0' was not found in the scheduler lists");
	const entry: BacktraceEntry = { report };
	const cached = new Map<string, BacktraceEntry>([['task|671100096|PC', entry]]);
	const { dom, document } = setup(cached);

	const btFolds = Array.from(document.querySelectorAll<HTMLDetailsElement>('.gdr-bt-fold'));
	assert.equal(btFolds.length, 3, 'every task row gets an expandable PC backtrace control');
	assert.equal(btFolds[0].dataset.btTarget, '0x28002cc0');
	assert.equal(btFolds[1].dataset.btTarget, '0x28000f80');
	assert.equal(btFolds[2].dataset.btTarget, '0x28003000');

	const rows = Array.from(btFolds[0].querySelectorAll('.gdr-bt-row'));
	assert.equal(rows.length, 3);
	assert.equal(btFolds[0].querySelector('.gdr-bt-method'), null, 'method tags are not shown in the frame list');
	assert.equal(btFolds[0].querySelector('.gdr-bt-src'), null, 'filename:line suffix is omitted after symbols');
	assert.ok(!rows[0].textContent?.includes('port.c:102'), 'frame text shows symbol only without file:line');
	assert.equal(btFolds[0].querySelector('.gdr-bt-footer'), null, 'diagnostic report footer is omitted');
	assert.equal(btFolds[0].querySelector('.gdr-bt-notes'), null, 'raw log messages are omitted');
	assert.ok(!btFolds[0].textContent?.includes('was not found'), 'raw backend logs are not rendered');
	assert.equal(btFolds[0].querySelector('.gdr-popover-knob'), null, 'redundant drag knob is removed');
	assert.ok(btFolds[0].querySelector('.gdr-popover-head[data-popover-drag]'), 'title bar is the drag handle');
	assert.ok(btFolds[0].querySelector('.gdr-popover-name'), 'task name truncates separately from status pill');
	assert.ok(btFolds[0].querySelector('[data-popover-close]'));
	assert.ok(btFolds[0].querySelector('[data-popover-resize] svg'), 'centered SVG chevron in bottom grip');
	assert.equal(renderBacktraceBody({ error: 'internal error detail' }), '<p class="gdr-bt-empty">(empty)</p>');
	dom.window.close();
});

test('expanding a task PC requests its backtrace on demand and updates live frames without collapsing on link click', () => {
	const { dom, document, messages } = setup();
	const fold = document.querySelector<HTMLDetailsElement>('.gdr-bt-fold')!;
	assert.equal(fold.dataset.btLoaded, '0');

	// Clicking the PC source link navigates without toggling the popover open.
	const pcLink = fold.querySelector<HTMLAnchorElement>('summary a.gdr-source')!;
	pcLink.click();
	assert.equal(messages.at(-1).type, 'openSource');
	assert.equal(fold.open, false);

	// Expanding the disclosure posts a backtrace request for the task's TCB address.
	fold.open = true;
	fold.dispatchEvent(new dom.window.Event('toggle'));
	assert.deepEqual(messages.at(-1), {
		type: 'backtrace',
		target: '0x28002cc0',
		key: 'task|671100096|PC',
		sessionId: 's1'
	});

	// Host pushes rendered nested backtrace body; client updates the popover in place.
	const entry: BacktraceEntry = { report: makeNestedBacktrace() };
	const status = backtraceStatus(entry);
	dom.window.dispatchEvent(
		new dom.window.MessageEvent('message', {
			origin: dom.window.origin,
			data: {
				type: 'backtrace',
				sessionId: 's1',
				key: 'task|671100096|PC',
				generation: 1,
				statusText: status.text,
				statusKind: status.kind,
				bodyHtml: renderBacktraceBody(entry)
			}
		})
	);
	assert.equal(fold.dataset.btLoaded, '1');
	assert.equal(fold.querySelectorAll('.gdr-bt-row').length, 3);
	assert.equal(fold.querySelector('[data-bt-status]')?.textContent, '3 frames');

	// Clicking a frame opens its source location.
	fold.querySelector<HTMLAnchorElement>('.gdr-bt-list a.gdr-source')!.click();
	assert.equal(messages.at(-1).type, 'openSource');
	assert.equal(messages.at(-1).file, irqSource.file);
	dom.window.close();
});

test('popover knob detaches to float independently on scroll, close collapses, and bottom grip fixes height', () => {
	const { dom, document, messages, apply } = setup();
	const fold = document.querySelector<HTMLDetailsElement>('.gdr-bt-fold')!;
	const summary = fold.querySelector<HTMLElement>('summary')!;
	const menu = fold.querySelector<HTMLElement>('.gdr-waiter-menu')!;
	let summaryTop = 120;
	summary.getBoundingClientRect = () => ({ left: 80, top: summaryTop, bottom: summaryTop + 20, right: 200, width: 120, height: 20 } as DOMRect);
	menu.getBoundingClientRect = () => ({ left: 80, top: 140, bottom: 300, right: 420, width: 340, height: 160 } as DOMRect);

	// 1. Anchored by default: follows cell scroll, sizes to content (no forced inline width), and auto-fits height.
	fold.open = true;
	fold.dispatchEvent(new dom.window.Event('toggle'));
	assert.equal(menu.style.top, '140px');
	assert.equal(menu.style.width, '', 'width follows CSS content sizing rather than a forced wide default');
	assert.equal(menu.style.height, '', 'auto-fits full list height by default');
	summaryTop = 60;
	document.querySelector('.gdr-body')!.dispatchEvent(new dom.window.Event('scroll'));
	assert.equal(menu.style.top, '80px', 'anchored popover follows body scroll');

	// 2. Dragging the title bar detaches the popover so it floats independently of scroll.
	const head = menu.querySelector<HTMLElement>('.gdr-popover-head[data-popover-drag]')!;
	head.dispatchEvent(new dom.window.PointerEvent('pointerdown', { clientX: 400, clientY: 145, pointerId: 2, button: 0, bubbles: true, cancelable: true }));
	dom.window.dispatchEvent(new dom.window.PointerEvent('pointermove', { clientX: 520, clientY: 215, pointerId: 2 }));
	dom.window.dispatchEvent(new dom.window.PointerEvent('pointerup', { clientX: 520, clientY: 215, pointerId: 2 }));
	assert.ok(menu.classList.contains('gdr-popover-detached'));
	assert.equal(menu.style.left, '200px');
	assert.equal(menu.style.top, '210px');

	// Scrolling no longer moves the detached popover.
	summaryTop = 10;
	document.querySelector('.gdr-body')!.dispatchEvent(new dom.window.Event('scroll'));
	assert.equal(menu.style.top, '210px', 'detached popover stays pinned on scroll');

	// 3. Dragging bottom grip fixes height (overflow darkens the grip); double-click resets to auto.
	const body = menu.querySelector<HTMLElement>('.gdr-popover-body')!;
	let bodyScrollH = 120;
	let bodyClientH = 120;
	Object.defineProperty(body, 'scrollHeight', { get: () => bodyScrollH, configurable: true });
	Object.defineProperty(body, 'clientHeight', { get: () => bodyClientH, configurable: true });
	const grip = menu.querySelector<HTMLElement>('[data-popover-resize]')!;
	assert.equal(grip.classList.contains('gdr-popover-grip-overflow'), false);
	bodyClientH = 60;
	grip.dispatchEvent(new dom.window.PointerEvent('pointerdown', { clientY: 300, pointerId: 3, button: 0, bubbles: true, cancelable: true }));
	dom.window.dispatchEvent(new dom.window.PointerEvent('pointermove', { clientY: 240, pointerId: 3 }));
	dom.window.dispatchEvent(new dom.window.PointerEvent('pointerup', { clientY: 240, pointerId: 3 }));
	assert.ok(menu.classList.contains('gdr-popover-fixed-height'));
	assert.equal(menu.style.height, '100px');
	assert.equal(menu.style.maxHeight, '');
	assert.ok(grip.classList.contains('gdr-popover-grip-overflow'), 'clipped frames darken bottom grip');

	// 4. Host refresh at a new stop preserves detached floating position + fixed height and re-fetches backtrace.
	messages.length = 0;
	apply(2);
	const refreshedFold = document.querySelector<HTMLDetailsElement>('.gdr-bt-fold')!;
	const refreshedMenu = refreshedFold.querySelector<HTMLElement>('.gdr-waiter-menu')!;
	assert.equal(refreshedFold.open, true);
	assert.ok(refreshedMenu.classList.contains('gdr-popover-detached'));
	assert.equal(refreshedMenu.style.left, '200px');
	assert.equal(refreshedMenu.style.top, '210px');
	assert.equal(refreshedMenu.style.height, '100px');
	assert.equal(messages.at(-1).type, 'backtrace', 'open backtrace popover refreshes on new stop');

	// Double-click grip restores auto-fit height.
	refreshedMenu.querySelector<HTMLElement>('[data-popover-resize]')!.dispatchEvent(
		new dom.window.MouseEvent('dblclick', { bubbles: true, cancelable: true })
	);
	assert.equal(refreshedMenu.style.height, '');

	// 5. Close button collapses the popover and clears detached state (pointerdown on close never re-detaches).
	const closeBtn = refreshedMenu.querySelector<HTMLElement>('[data-popover-close]')!;
	closeBtn.dispatchEvent(new dom.window.PointerEvent('pointerdown', { clientX: 500, clientY: 215, pointerId: 4, button: 0, bubbles: true, cancelable: true }));
	closeBtn.click();
	assert.equal(refreshedFold.open, false);
	dom.window.close();
});

test('RtosViewsPanel caches backtraces per stop, clears on new generation, and drops stale replies', async () => {
	const vscode = {
		window: {
			showErrorMessage: () => undefined,
			showWarningMessage: () => undefined,
			showQuickPick: async () => undefined
		},
		commands: { executeCommand: async () => undefined },
		Uri: {
			joinPath: (...parts: unknown[]) => ({ toString: () => parts.map(String).join('/') })
		}
	};
	const cache = new Map<string, any>();
	const builtins: Record<string, unknown> = { 'node:crypto': crypto, 'node:fs': fs, 'node:os': os, 'node:path': path };
	function load(file: string): any {
		const absolute = path.resolve(file);
		if (cache.has(absolute)) { return cache.get(absolute); }
		const module = { exports: {} };
		cache.set(absolute, module.exports);
		const code = ts.transpileModule(readFileSync(absolute, 'utf8'), {
			compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS }
		}).outputText;
		runInNewContext(
			code,
			{
				module,
				exports: module.exports,
				Buffer,
				setTimeout,
				clearTimeout,
				require: (name: string) => {
					if (name === 'vscode') { return vscode; }
					if (name.startsWith('./') && name.endsWith('.ts')) {
						return load(path.resolve(path.dirname(absolute), name));
					}
					assert.ok(Object.hasOwn(builtins, name), `Unexpected module: ${name}`);
					return builtins[name];
				}
			},
			{ filename: absolute }
		);
		return module.exports;
	}

	const { RtosViewsPanel } = load('src/panel.ts') as { RtosViewsPanel: typeof Panel };
	let generation = 1;
	const snapshotData = {
		tasks: {
			headers: ['Name', 'State', 'PC', 'Addr'],
			rows: [['gdr_recm *', 'Running', '<gdr_recursive_hold+24>', '0x28002cc0']],
			ids: [0x28002cc0],
			value_rows: [['gdr_recm *', 'Running', 0x10000488, 0x28002cc0]],
			source_rows: [[null, null, taskSource, null]],
			columns: [
				{ key: 'Name', kind: 'text' },
				{ key: 'State', kind: 'text' },
				{ key: 'PC', kind: 'pc' },
				{ key: 'Addr', kind: 'addr' }
			],
			messages: [],
			elastic: []
		},
		tables: {},
		system: null,
		heap: null,
		capabilities: null
	};
	const btCalls: string[] = [];
	let pendingResolve: ((env: GdrEnvelope | null) => void) | null = null;
	const btEnvelope = (sym: string): GdrEnvelope => ({
		schema_version: 3,
		ok: true,
		data: {
			task: 'gdr_recm',
			address: 0x28002cc0,
			architecture: 'armv8-m.mainline',
			abi: 'aapcs32-vfp',
			method: 'cfi',
			source: 'current',
			complete: true,
			stop_reason: 'task-exit',
			truncated: false,
			max_depth: 32,
			bounds: 'recorded',
			frames: [{ index: 0, pc: 0x10000488, symbol: sym, source: taskSource, method: 'seed' }],
			messages: []
		}
	});
	let deferNext = false;
	const posted: any[] = [];
	let onMessage: ((message: unknown) => void) | undefined;

	const panel = new RtosViewsPanel({
		log: () => undefined,
		extensionUri: { toString: () => 'ext' } as any,
		costOptions: () => ({ noHeap: false, noDetails: false }),
		recordDir: () => '',
		fetchSnapshot: async () => ({
			schema_version: 3,
			ok: true,
			rtos: 'freertos',
			rtos_version: '11.1.0',
			generation,
			data: snapshotData
		}),
		fetchHeapMap: async () => null,
		fetchBacktrace: async (_sid, _fid, target) => {
			btCalls.push(target);
			if (deferNext) {
				deferNext = false;
				return await new Promise<GdrEnvelope | null>((resolve) => {
					pendingResolve = resolve;
				});
			}
			return btEnvelope(`fn_gen${generation}`);
		},
		layoutStore: { read: () => null, write: () => undefined }
	});

	panel.resolveWebviewView({
		webview: {
			cspSource: "'self'",
			options: {},
			html: '',
			asWebviewUri: (u: any) => u,
			postMessage: async (msg: unknown) => { posted.push(msg); return true; },
			onDidReceiveMessage: (cb: (msg: unknown) => void) => { onMessage = cb; return { dispose: () => undefined }; }
		},
		onDidDispose: () => ({ dispose: () => undefined })
	} as any);

	panel.sessionStarted('s1', 'QEMU');
	panel.firstStackTrace('s1', 1);
	panel.refreshNow('s1');
	await new Promise((r) => setTimeout(r, 10));

	// First request fetches and caches for generation 1.
	onMessage!({ type: 'backtrace', target: '0x28002cc0', key: 'task|671100096|PC', sessionId: 's1' });
	await new Promise((r) => setTimeout(r, 10));
	assert.deepEqual(btCalls, ['0x28002cc0']);
	assert.equal(posted.filter((m) => m.type === 'backtrace').length, 1);
	assert.ok(posted.at(-1).bodyHtml.includes('fn_gen1'));

	// Second request at the same generation hits cache without re-querying GDB.
	onMessage!({ type: 'backtrace', target: '0x28002cc0', key: 'task|671100096|PC', sessionId: 's1' });
	await new Promise((r) => setTimeout(r, 10));
	assert.equal(btCalls.length, 1);
	assert.equal(posted.filter((m) => m.type === 'backtrace').length, 2);

	// New stop generation clears cache; stale reply from previous generation is dropped.
	generation = 2;
	panel.refreshNow('s1');
	await new Promise((r) => setTimeout(r, 10));
	deferNext = true;
	onMessage!({ type: 'backtrace', target: '0x28002cc0', key: 'task|671100096|PC', sessionId: 's1' });
	await new Promise((r) => setTimeout(r, 5));
	assert.equal(btCalls.length, 2);

	generation = 3;
	panel.refreshNow('s1');
	await new Promise((r) => setTimeout(r, 10));
	const beforeStale = posted.filter((m) => m.type === 'backtrace').length;
	pendingResolve!(btEnvelope('stale_gen2'));
	await new Promise((r) => setTimeout(r, 10));
	assert.equal(posted.filter((m) => m.type === 'backtrace').length, beforeStale, 'stale backtrace reply is dropped');

	onMessage!({ type: 'backtrace', target: '0x28002cc0', key: 'task|671100096|PC', sessionId: 's1' });
	await new Promise((r) => setTimeout(r, 10));
	assert.equal(btCalls.length, 3);
	assert.ok(posted.at(-1).bodyHtml.includes('fn_gen3'));
	panel.sessionEnded('s1');
});
