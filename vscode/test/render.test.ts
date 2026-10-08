/**
 * Unit tests for the pure panel renderer.
 *
 * The renderer is the single place where envelope data becomes markup, so the
 * escaping boundary, the column alignment, the percent bars, the waiter fold
 * and the hint bars are all asserted here with synthetic envelopes. A
 * real-session render assertion lives in `test/integration/gdr-session.test.ts`.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { ObjectTableData, PanelSnapshot, SnapshotData } from '../src/snapshot.ts';
import type { PanelState } from '../src/webview/panelState.ts';
import {
	renderApp,
	renderAddress,
	renderSourceLink,
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
				{ key: 'Name', kind: 'text', align: 'left', elastic: true, doc: 'task name' },
				{ key: 'State', kind: 'text', align: 'left', elastic: false, doc: '' },
				{ key: 'Prio', kind: 'int', align: 'right', elastic: false, doc: '' },
				{ key: 'Stack', kind: 'int', align: 'right', elastic: false, doc: '' },
				{ key: 'Used', kind: 'int', align: 'right', elastic: false, doc: '' },
				{ key: 'Waiters', kind: 'waiters', align: 'left', elastic: true, doc: '' },
				{ key: 'Addr', kind: 'addr', align: 'left', elastic: false, doc: '' }
			],
			value_rows: [
				['main *', 'running', 10, 2048, 512, ['alpha', 'beta', 'gamma'], 0x20001234],
				['worker', 'suspend', 20, 4096, 3900, null, 0x20001568]
			],
			ids: ['0x20001234', '0x20001568']
		},
		objects: { headers: ['Kind', 'Count'], rows: [['semaphore', '3']], messages: [], elastic: [] },
		system: {
			pairs: [
				['Kernel version', '4.0.5'],
				['Current task', 'main']
			]
		},
		heap: { pairs: [['Algorithm', 'small']], messages: [], table: null, tableTitle: '' },
		capabilities: {
			kinds: {
				queue: { enumerable: false, detail: 'needs configQUEUE_REGISTRY_SIZE', columns: [], note: '' },
				semaphore: { enumerable: true, detail: '', columns: ['Name'], note: '' }
			},
			extra: ['nothing extra']
		},
		checks: {
			findings: [
				{ name: 'TaskCount', verdict: 'ok', detail: '2 tasks', scope: 'system' },
				{ name: 'ListHealth', verdict: 'fail', detail: 'dangling node', scope: 'system' }
			],
			counts: { ok: 1, fail: 1 }
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

test('numeric columns render right-aligned and the identity column carries its id', () => {
	const html = renderApp(state());
	assert.ok(html.includes('<th>Prio</th>'), 'headers are left-aligned');
	assert.ok(!html.includes('<th class="gdr-right">'), 'no header takes the data alignment');
	assert.ok(html.includes('<td class="gdr-right">10</td>'));
	assert.ok(html.includes('data-id="0x20001234"'));
});

test('address cells become navigation anchors with the raw address', () => {
	const html = renderApp(state());
	assert.ok(html.includes('<a href="#" data-address="0x20001234">0x20001234</a>'));
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
	// Two or fewer names show inline; more than two collapse.
	const inline = renderWaiters('2 (a, b)', ['alpha', 'beta']);
	assert.equal(inline, 'alpha, beta');
	const folded = renderWaiters('3 (a, b, c)', ['alpha', 'beta', 'gamma']);
	assert.ok(folded.includes('<details class="gdr-waiter-fold">'));
	assert.ok(folded.includes('<summary>alpha, beta (+1)</summary>'));
	assert.ok(folded.includes('<li>gamma</li>'));
	const single = renderWaiters('1 (idle)', null);
	assert.equal(single, '1 (idle)', 'no names means the text stays as is');
	assert.ok(renderApp(state()).includes('gdr-waiter-fold'));
});

test('hint bars collect warnings, table messages and capability gaps', () => {
	const snapshot = panelSnapshot();
	const hints = snapshotHints(snapshot);
	const texts = hints.map((hint) => hint.text);
	assert.ok(texts.includes('reflash detected, caches cleared'));
	assert.ok(texts.includes('high-water gated by config'));
	assert.ok(texts.some((text) => text.includes('queue: needs configQUEUE_REGISTRY_SIZE')));
	assert.ok(texts.includes('nothing extra'));
	const html = renderApp(state());
	assert.ok(html.includes('gdr-hint-warn'), 'a missing capability hint is marked a warning');
});

test('check verdicts are coloured by severity', () => {
	const html = renderApp(state());
	assert.ok(html.includes('gdr-verdict-ok'));
	assert.ok(html.includes('gdr-verdict-fail'));
	assert.ok(html.includes('dangling node'));
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

test('the heap table is titled by what it lists', () => {
	// Reason: the same panel shows free-list blocks (FreeRTOS) and used
	// blocks per thread (RT-Thread); one hard-coded title would mislabel one.
	const snapshot: PanelSnapshot = panelSnapshot();
	snapshot.data.heap = {
		pairs: [['Algorithm', 'small_mem']],
		messages: [],
		table: { headers: ['Thread', 'Blocks', 'Bytes'], rows: [['main', '2', '128']], messages: [], elastic: ['Thread'] },
		tableTitle: 'Heap used by thread'
	};
	const html = renderApp({
		sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot }],
		activeId: 's1'
	});
	assert.ok(html.includes('Heap used by thread'));
	assert.ok(!html.includes('Free blocks'));
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
			value_rows: [['worker', 4096, 1024, 3072]]
		})
	);
	assert.ok(html.includes('<th class="gdr-bar-cell">HighWater</th>'), 'the widened header');
	assert.ok(html.includes('class="gdr-bar-cell gdr-pct-25" title="25% of the stack">1024</td>'));
	assert.ok(html.includes('class="gdr-bar-cell gdr-pct-75" title="75% of the stack">3072</td>'));
});

test('a task entry with a known source renders as a link to that line', () => {
	const html = renderApp(
		stateWithTasks({
			headers: ['Name', 'Entry'],
			rows: [['tshell', '<finsh_thread_entry>']],
			messages: [],
			elastic: ['Name'],
			value_rows: [['tshell', 0x600bcc00]],
			source_rows: [[null, { file: '/src/shell "x".c', line: 1700 }]]
		})
	);
	assert.ok(
		html.includes(
			'<a href="#" class="gdr-source" data-source-file="/src/shell &quot;x&quot;.c" ' +
				'data-source-line="1700" title="/src/shell &quot;x&quot;.c:1700">' +
				'&lt;finsh_thread_entry&gt;</a>'
		)
	);
});

test('a source link escapes its file path and keeps the symbol text', () => {
	const html = renderSourceLink('<main>', { file: '/a<b>.c', line: 3 });
	assert.ok(html.includes('data-source-file="/a&lt;b&gt;.c"'));
	assert.ok(html.endsWith('>&lt;main&gt;</a>'));
});

test('renderAddress escapes the address text', () => {
	assert.ok(renderAddress('<script>0x0</script>').includes('&lt;script&gt;'));
});
