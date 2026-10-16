/**
 * Unit tests for the pure panel renderer.
 *
 * The renderer is the single place where envelope data becomes markup, so the
 * escaping boundary, the column alignment, the percent bars, the waiter fold,
 * the source links, the verdict columns, the layout and the change highlight
 * are all asserted here with synthetic envelopes.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { ObjectTableData, PanelSnapshot, SnapshotData, SourceRef } from '../src/snapshot.ts';
import type { PanelLayout } from '../src/webview/messages.ts';
import type { PanelState } from '../src/webview/panelState.ts';
import {
	changeKey,
	renderApp,
	renderAddress,
	renderSourceLink,
	renderSystem,
	renderWaiters,
	snapshotHints,
	stackPercent
} from '../src/webview/render.ts';

function panelSnapshot(overrides: Partial<PanelSnapshot> = {}): PanelSnapshot {
	const data: SnapshotData = {
		tasks: {
			headers: ['Name', 'State', 'Prio', 'Stack', 'Used', 'Waiters', 'Addr'],
			rows: [
				['main *', 'running', '10', '2048', '512', '3 (a, b, c)', '0x20001234'],
				['worker', 'suspend', '20', '4096', '3900', '1 (idle)', '0x20001568']
			],
			messages: ['high-water gated by config'],
			elastic: ['Name'],
			columns: [
				{ key: 'Name', kind: 'text', align: 'left', elastic: true, doc: 'task name', group: 'basic' },
				{ key: 'State', kind: 'text', align: 'left', elastic: false, doc: '', group: 'basic' },
				{ key: 'Prio', kind: 'int', align: 'right', elastic: false, doc: '', group: 'basic' },
				{ key: 'Stack', kind: 'int', align: 'right', elastic: false, doc: '', group: 'basic' },
				{ key: 'Used', kind: 'int', align: 'right', elastic: false, doc: '', group: 'basic' },
				{ key: 'Waiters', kind: 'waiters', align: 'left', elastic: true, doc: '', group: 'basic' },
				{ key: 'Addr', kind: 'addr', align: 'left', elastic: false, doc: '', group: 'basic' }
			],
			value_rows: [
				['main *', 'running', 10, 2048, 512, ['alpha', 'beta', 'gamma'], 0x20001234],
				['worker', 'suspend', 20, 4096, 3900, null, 0x20001568]
			],
			ids: ['0x20001234', '0x20001568']
		},
		tables: {
			semaphore: {
				headers: ['Name', 'Address', 'Value', 'OffsetCheck'],
				rows: [['sem', '0x2000', '3', 'ok']],
				messages: [],
				elastic: ['Name'],
				kind: 'semaphore',
				columns: [
					{ key: 'Name', kind: 'text', align: 'left', elastic: true, doc: '', group: 'basic' },
					{ key: 'Address', kind: 'addr', align: 'left', elastic: false, doc: '', group: 'basic' },
					{ key: 'Value', kind: 'text', align: 'left', elastic: false, doc: '', group: 'basic' },
					{ key: 'OffsetCheck', kind: 'text', align: 'left', elastic: false, doc: '', group: 'diag' }
				],
				ids: ['0x2000']
			}
		},
		system: {
			pairs: [
				['Current task', 'main'],
				['Scheduler state', 'Unlocked']
			]
		},
		heap: {
			pairs: [
				['Algorithm', 'small'],
				['Blocks', '3 used, 1 free, 4 total']
			],
			messages: [],
			sections: [
				{
					title: 'Checks',
					rows: [
						{ key: 'HeapChain', value: 'ok', severity: 'ok' },
						{ key: 'HeapAccounting', value: 'fail: snapshot used 9 B != walk 8 B', severity: 'fail' }
					]
				}
			]
		},
		capabilities: {
			kinds: {
				queue: { enumerable: false, detail: 'needs configQUEUE_REGISTRY_SIZE', columns: [], note: '' },
				semaphore: { enumerable: true, detail: '', columns: ['Name'], note: '' }
			},
			extra: ['nothing extra']
		}
	};
	return {
		rtos: 'rtthread',
		rtosVersion: '4.0.5',
		gdrVersion: '2026.3',
		generation: 3,
		elapsedMs: 12.5,
		collectedAt: 0,
		warnings: ['reflash detected, caches cleared'],
		noHeap: false,
		noDetails: false,
		data,
		...overrides
	};
}

function state(overrides: Partial<PanelState> = {}): PanelState {
	return {
		sessions: [
			{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot: panelSnapshot() },
			{ id: 's2', name: 'bare-metal', status: 'waiting', message: 'waiting for the first stop' }
		],
		activeId: 's1',
		...overrides
	};
}

test('the tab strip lists every session with its status dot', () => {
	const html = renderApp(state());
	assert.ok(html.includes('firmware'));
	assert.ok(html.includes('bare-metal'));
	assert.ok(html.includes('gdr-tab gdr-active'));
	assert.ok(html.includes('gdr-ok'));
	assert.ok(html.includes('gdr-waiting'));
});

test('numeric columns render right-aligned and the row carries its identity', () => {
	const html = renderApp(state());
	assert.ok(html.includes('<th data-col="Prio" draggable="true">Prio'), 'headers are left-aligned');
	assert.ok(!html.includes('<th class="gdr-right"'), 'no header takes the data alignment');
	assert.ok(html.includes('<td class="gdr-right">10</td>'));
	assert.ok(html.includes('data-id="0x20001234"'));
});

test('address cells become navigation anchors with the raw address', () => {
	const html = renderApp(state());
	assert.ok(html.includes('data-address="0x20001234" data-pointer="0x20001234"'));
	assert.ok(html.includes('data-address="0x20001568"'));
});

test('used-by-stack cells render a percentage bar as a background class', () => {
	const html = renderApp(state());
	assert.ok(html.includes('class="gdr-right gdr-bar-cell gdr-pct-25" title="25% of the stack">512</td>'));
	assert.ok(
		html.includes('class="gdr-right gdr-bar-cell gdr-pct-95 gdr-pct-hot" title="95% of the stack">3900</td>'),
		'a 95%-used stack is flagged hot'
	);
});

test('the panel never emits an inline style, which its CSP would refuse', () => {
	// Reason: the webview allows styles only from its own stylesheet, so an
	// inline `style=` attribute is dropped silently and the bar never shows.
	assert.ok(!/\sstyle=/.test(renderApp(state())));
});

test('the body names its session so a refresh can keep the scroll position', () => {
	const html = renderApp(state());
	assert.ok(html.includes('<main class="gdr-body" data-session="s1">'));
});

test('a waiter list with several names folds into a disclosure', () => {
	const inline = renderWaiters('2 (a, b)', ['alpha', 'beta']);
	assert.ok(inline.includes('<summary>2</summary>'));
	const folded = renderWaiters('3 (a, b, c)', ['alpha', 'beta', 'gamma']);
	assert.ok(folded.includes('<details class="gdr-waiter-fold"'));
	assert.ok(folded.includes('<summary>3</summary>'));
	assert.ok(folded.includes('<li>gamma</li>'));
	const single = renderWaiters('1 (idle)', null);
	assert.equal(single, '1 (idle)', 'no names means the text stays as is');
	assert.ok(renderApp(state()).includes('gdr-waiter-fold'));
});

test('message block contains only actionable build configuration hints', () => {
	const snapshot = panelSnapshot({ data: { ...panelSnapshot().data, buildHints: ['enable configUSE_TRACE_FACILITY'] } });
	const texts = snapshotHints(snapshot).map((hint) => hint.text);
	assert.deepEqual(texts, ['enable configUSE_TRACE_FACILITY']);
	const html = renderApp({ sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot }], activeId: 's1' });
	assert.ok(html.includes('gdr-hints-grip'));
	assert.ok(html.includes('aria-label="RTOS build configuration hints"'));
	assert.ok(!html.includes('reflash detected'));
});

test('the checks card is gone: verdicts ride the heap table row itself', () => {
	const html = renderApp(state());
	assert.ok(!html.includes('<h2>Checks</h2>'), 'no standalone checks card');
	assert.ok(!html.includes('gdr-verdict-table'), 'no standalone verdicts table');
	assert.ok(html.includes('<table class="gdr-table gdr-heap-table">'));
	assert.ok(html.includes('<th class="gdr-diag-col" data-col="HeapAccounting" draggable="true">HeapAccounting<span class="gdr-col-grip" aria-hidden="true"></span></th>'));
	assert.ok(html.includes('gdr-verdict gdr-verdict-fail'));
	assert.ok(html.includes('snapshot used 9 B != walk 8 B'));
	const head = html.split('<table class="gdr-table gdr-heap-table">')[1].split('</thead>')[0];
	assert.ok(head.indexOf('data-col="Blocks"') < head.indexOf('data-col="HeapChain"'), 'basics keep ahead of the checks');
});

test('the system summary is one header row and one value row, without a kernel version', () => {
	const html = renderApp(state());
	assert.ok(html.includes('<table class="gdr-table gdr-system-table">'));
	assert.ok(html.includes('<th>Current task</th><th>Scheduler state</th>'));
	assert.ok(html.includes('<td>main</td><td>Unlocked</td>'));
	assert.ok(!html.includes('Kernel version'));
	assert.equal(renderSystem([]), '', 'an empty summary draws nothing');
});

test('the heap card shows its basics in a header/value row and the block count', () => {
	const html = renderApp(state());
	assert.ok(html.includes('<table class="gdr-table gdr-heap-table">'));
	assert.ok(html.includes('data-col="Algorithm" draggable="true">Algorithm'));
	assert.ok(html.includes('data-col="Blocks" draggable="true">Blocks'));
	assert.ok(html.includes('<td>3 used, 1 free, 4 total</td>'));
	assert.ok(!html.includes('Holes'), 'holes left the heap card');
	assert.ok(!html.includes('Thread occupancy'), 'owner occupancy left the heap card');
});

test('the heap card offers the heap map on demand and draws a canvas once asked', () => {
	const html = renderApp(state());
	assert.ok(html.includes('data-disclosure="heap-map"'), 'the section shell is always present');
	assert.ok(!html.includes('data-heap-dump'), 'no draw button remains');
	assert.ok(!html.includes('data-heap-canvas'), 'nothing is drawn until requested');
	assert.ok(html.includes('<details class="gdr-heap-fold" data-disclosure="heap-map">'), 'unrequested starts collapsed');

	const drawn = renderApp(state(), { heapCanvas: true });
	assert.ok(drawn.includes('data-heap-canvas="1"'));
	assert.ok(!drawn.includes('data-heap-readout'), 'no fixed readout below the bitmap');
	assert.ok(!drawn.includes('heap-hover-readout'), 'no dangling readout accessibility reference');
	assert.ok(drawn.includes('data-heap-tooltip="1"'));
	assert.ok(drawn.includes('data-heap-addr-start="1"'), 'the start label ships as a shell');
	assert.ok(drawn.includes('data-heap-addr-end="1"'), 'the end label ships as a shell');
	assert.ok(drawn.includes('data-heap-legend="1"'), 'the ranking legend ships as a shell');
	assert.ok(drawn.includes('data-heap-grip="1"'), 'the resize grip ships as a shell');
	assert.ok(drawn.includes('data-heap-resize="1"'), 'the live row-bytes label ships as a shell');
	assert.ok(drawn.includes('data-heap-corner="1"'), 'the scale corner ships as a shell');
	assert.ok(!drawn.includes('data-heap-zoom'), 'no zoom controls remain');
	assert.ok(!drawn.includes('data-heap-axis'), 'no gutter canvas remains');
});

test('the heap card explains a cost-switched-off heap', () => {
	// With --no-heap GDR serializes the heap section as null; the card
	// advertises the switch instead of pretending nothing is missing.
	const snapshot: PanelSnapshot = panelSnapshot({ noHeap: true });
	snapshot.data.heap = null;
	const html = renderApp({
		sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot }],
		activeId: 's1'
	});
	assert.ok(html.includes('hidden by the heap cost switch'));
});

test('a running target keeps the snapshot under a stale banner', () => {
	const html = renderApp({
		sessions: [{ id: 's1', name: 'firmware', status: 'running', message: null, snapshot: panelSnapshot() }],
		activeId: 's1'
	});
	assert.ok(html.includes('target running — showing the last stop'));
});

test('an error session shows its reason without a snapshot', () => {
	const html = renderApp({
		sessions: [{ id: 's1', name: 'firmware', status: 'error', message: 'probe found nothing' }],
		activeId: 's1'
	});
	assert.ok(html.includes('GDR could not initialize'));
	assert.ok(html.includes('probe found nothing'));
});

test('adversarial task names cannot inject markup', () => {
	const snapshot = panelSnapshot();
	const data = snapshot.data;
	assert.ok(data.tasks);
	data.tasks.rows[0][0] = '<img src=x onerror=alert(1)>';
	data.tasks.value_rows![0][0] = data.tasks.rows[0][0];
	const html = renderApp({
		sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot }],
		activeId: 's1'
	});
	assert.ok(!html.includes('<img'), 'the adversarial name stays text');
	assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
});

test('the stack percentage refuses a missing or empty stack and clamps to 0..100', () => {
	assert.equal(stackPercent(512, null), null);
	assert.equal(stackPercent(null, 2048), null);
	assert.equal(stackPercent(10, 0), null);
	assert.equal(stackPercent(512, 2048), 25);
	assert.equal(stackPercent(4096, 2048), 100);
	assert.equal(stackPercent(-4, 2048), 0);
});

/** The app rendered with one replaced tasks table, everything else as in state(). */
function stateWithTasks(tasks: ObjectTableData): PanelState {
	const snapshot = panelSnapshot();
	snapshot.data.tasks = tasks;
	return {
		sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot }],
		activeId: 's1'
	};
}

test('the high-water column shares the stack bar', () => {
	const html = renderApp(
		stateWithTasks({
			headers: ['Name', 'Stack', 'Used', 'HighWater'],
			rows: [['worker', '4096', '1024', '3072']],
			messages: [],
			elastic: ['Name'],
			columns: [
				{ key: 'Name', kind: 'text', align: 'left', elastic: true, doc: '', group: 'basic' },
				{ key: 'Stack', kind: 'int', align: 'right', elastic: false, doc: '', group: 'basic' },
				{ key: 'Used', kind: 'int', align: 'right', elastic: false, doc: '', group: 'basic' },
				{ key: 'HighWater', kind: 'int', align: 'right', elastic: false, doc: '', group: 'basic' }
			],
			value_rows: [['worker', 4096, 1024, 3072]]
		})
	);
	assert.ok(html.includes('<th class="gdr-bar-cell" data-col="HighWater" draggable="true">HighWater'), 'the widened header');
	assert.ok(html.includes('class="gdr-right gdr-bar-cell gdr-pct-25" title="25% of the stack">1024</td>'));
	assert.ok(html.includes('class="gdr-right gdr-bar-cell gdr-pct-75" title="75% of the stack">3072</td>'));
});

test('a PC or Entry with a known source renders as a link carrying the recorded name', () => {
	const source: SourceRef = { file: '/src/shell "x".c', name: 'applications/shell "x".c', line: 1700 };
	const html = renderApp(
		stateWithTasks({
			headers: ['Name', 'Entry'],
			rows: [['tshell', '<finsh_thread_entry>']],
			messages: [],
			elastic: ['Name'],
			value_rows: [['tshell', 0x600bcc00]],
			source_rows: [[null, source]]
		})
	);
	assert.ok(
		html.includes(
			'<a href="#" class="gdr-source" data-source-file="/src/shell &quot;x&quot;.c" ' +
				'data-source-name="applications/shell &quot;x&quot;.c" ' +
				'data-source-line="1700" data-fallback-address="0x600bcc00" data-pointer="0x600bcc00" aria-label="&lt;finsh_thread_entry&gt; (0x600bcc00)">' +
				'&lt;finsh_thread_entry&gt;</a>'
		)
	);
});

test('a source link escapes its file path and keeps the symbol text', () => {
	const html = renderSourceLink('<main>', { file: '/a<b>.c', name: 'a<b>.c', line: 3 });
	assert.ok(html.includes('data-source-file="/a&lt;b&gt;.c"'));
	assert.ok(html.endsWith('>&lt;main&gt;</a>'));
});

test('renderAddress escapes the address text', () => {
	assert.ok(renderAddress('<script>0x0</script>').includes('&lt;script&gt;'));
});

test('the tables follow the saved order, with hidden tables and columns left out', () => {
	const layout: PanelLayout = {
		order: ['semaphore', 'task'],
		hiddenTables: ['heap'],
		tables: {
			task: { kind: 'task', widths: {}, hiddenColumns: ['Waiters'] }
		}
	};
	const html = renderApp(state(), { layout });
	assert.ok(html.indexOf('data-kind="semaphore"') < html.indexOf('data-kind="task"'), 'the saved order wins');
	assert.ok(!html.includes('data-kind="heap"'), 'a hidden table is not drawn');
	assert.ok(!html.includes('<th data-col="Waiters"'), 'a hidden column is not drawn');
	assert.ok(html.includes('<th data-col="Prio" draggable="true">Prio'), 'other columns stay');
});

test('a saved column width is written as a data attribute, never as a style', () => {
	const layout: PanelLayout = {
		order: [],
		hiddenTables: [],
		tables: { task: { kind: 'task', widths: { Name: 180 }, hiddenColumns: [] } }
	};
	const html = renderApp(state(), { layout });
	assert.ok(html.includes('data-col="Name" data-width="180"'));
	assert.ok(!/\sstyle=/.test(html));
});

test('a changed cell gets the change class and an unchanged one does not', () => {
	const changed = new Set([changeKey('task', '0x20001234', 'Prio')]);
	const html = renderApp(state(), { changed });
	assert.ok(html.includes('<td class="gdr-right gdr-changed">10</td>'), 'the changed Prio is marked');
	assert.ok(!html.includes('<td class="gdr-right gdr-changed">20</td>'), 'the other row is not');
});

test('the diagnostic column of a detail table is marked for the checkbox groups', () => {
	const html = renderApp(state());
	assert.ok(html.includes('<th class="gdr-diag-col" data-col="OffsetCheck" draggable="true">'));
	assert.ok(html.includes('<th data-col="Value" draggable="true">Value'), 'a basic column is not diagnostic');
});


test('System omits object kind counts but keeps system totals and scheduler state', () => {
 const kinds = ['device', 'event', 'mailbox', 'memheap', 'mempool', 'msgqueue', 'mutex', 'semaphore', 'task', 'timer'];
 const html = renderSystem([['Task number', '10'], ['Scheduler state', 'Unlocked'], ...kinds.map((key) => [key, '2'])]);
 for (const kind of kinds) { assert.ok(!html.includes(`<th>${kind}</th>`)); }
 assert.ok(html.includes('<th>Task number</th>'));
});

test('saved column order reorders the header and the associated values together', () => {
 const html = renderApp(state(), { layout: { order: [], hiddenTables: [], tables: { task: { kind: 'task', widths: {}, hiddenColumns: [], columnOrder: ['Prio', 'Name', 'Prio', 'stale'] } } } });
 const card = html.slice(html.indexOf('data-kind="task"'));
 assert.ok(card.indexOf('data-col="Prio"') < card.indexOf('data-col="Name"'));
 assert.ok(card.includes('<tr data-id="0x20001234"><td class="gdr-right">10</td><td>main *'));
});

test('tasks table renders the Addr identity column last by default', () => {
 const html = renderApp(state(), { layout: { order: [], hiddenTables: [], tables: {} } });
 const card = html.slice(html.indexOf('data-kind="task"'));
 assert.ok(card.indexOf('data-col="Name"') >= 0);
 assert.ok(card.indexOf('data-col="Name"') < card.indexOf('data-col="Addr"'));
 assert.ok(!card.includes('data-col="TCB"'));
});

test('a saved layout naming the pre-rename TCB column still renders Addr last', () => {
 const html = renderApp(state(), { layout: { order: [], hiddenTables: [], tables: { task: { kind: 'task', widths: {}, hiddenColumns: [], columnOrder: ['Name', 'TCB'] } } } });
 const card = html.slice(html.indexOf('data-kind="task"'));
 assert.ok(!card.includes('data-col="TCB"'));
 assert.ok(card.indexOf('data-col="Name"') < card.indexOf('data-col="Addr"'));
 assert.ok(card.includes('<tr data-id="0x20001234">'));
});

test('heap diagnostic columns render saved widths and resize grips', () => {
 const html = renderApp(state(), { layout: { order: [], hiddenTables: [], tables: { heap: { kind: 'heap', widths: { HeapAccounting: 260 }, hiddenColumns: [] } } } });
 assert.ok(html.includes('data-col="HeapAccounting" data-width="260" draggable="true">HeapAccounting<span class="gdr-col-grip"'));
});

test('heap diagnostic checkbox hides its actual column', () => {
 const html = renderApp(state(), { layout: { order: [], hiddenTables: [], tables: { heap: { kind: 'heap', widths: {}, hiddenColumns: ['HeapAccounting'] } } } });
 assert.ok(!html.includes('data-col="HeapAccounting"'));
 assert.ok(html.includes('data-col="HeapChain"'));
});


test('System removes redundant heap counters without changing its remaining fields', () => {
 const html = renderSystem([['Current task', 'main'], ['Heap allocator', 'small_mem'], ['Heap used', '10 B'], ['Heap total', '100 B'], ['Heap status', 'ok']]);
 assert.ok(html.includes('<th>Current task</th>'));
 assert.ok(html.includes('<th>Heap allocator</th>'));
 for (const key of ['Heap used', 'Heap total', 'Heap status']) { assert.ok(!html.includes(key)); }
});

test('heap map stays folded until the reader opens it', () => {
 const html = renderApp(state(), { heapCanvas: true });
 assert.ok(!html.includes('data-disclosure="heap-map" open'));
 assert.ok(html.includes('data-heap-tooltip="1"'));
 assert.ok(html.includes('data-heap-canvas="1" draggable="false"'));
});

test('Heap basic fields are selectable as well as diagnostic columns', () => {
 const html = renderApp(state(), { layout: { order: [], hiddenTables: [], tables: { heap: { kind: 'heap', widths: {}, hiddenColumns: ['Blocks'] } } } });
 assert.ok(html.includes('data-table="heap" data-column="Blocks"'));
 assert.ok(!html.includes('data-col="Blocks"'));
});


test('Display selection exposes separate drag sources for parents, children and card titles', () => {
 const html = renderApp(state());
 assert.ok(html.includes('<summary>Display selection'));
 assert.ok(!html.includes('Tables and columns'));
 assert.ok(html.includes('data-tree-column="Name" draggable="true"'));
 assert.ok(html.includes('<summary draggable="true" title="Drag to reorder tables">'));
 assert.ok(html.includes('<h2 class="gdr-card-title" draggable="true"'));
 assert.ok(!/section[^>]*draggable="true"/.test(html));
});
