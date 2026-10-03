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

import type { PanelSnapshot, SnapshotData } from '../src/snapshot.ts';
import type { PanelState } from '../src/webview/panelState.ts';
import { renderApp, renderAddress, renderPercentBar, renderWaiters, snapshotHints } from '../src/webview/render.ts';

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
		heap: { pairs: [['Algorithm', 'small']], messages: [], table: null },
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
	assert.ok(html.includes('<th class="gdr-right">Prio</th>'));
	assert.ok(html.includes('<td class="gdr-right">10</td>'));
	assert.ok(html.includes('data-id="0x20001234"'));
});

test('address cells become navigation anchors with the raw address', () => {
	const html = renderApp(state());
	assert.ok(html.includes('<a href="#" data-address="0x20001234">0x20001234</a>'));
	assert.ok(html.includes('data-address="0x20001568"'));
});

test('used-by-stack cells render a percentage bar', () => {
	const html = renderApp(state());
	assert.ok(html.includes('class="gdr-bar" title="25% used"'));
	assert.ok(html.includes('style="width:25%"'));
	assert.ok(html.includes('gdr-bar-hot'), 'a 95%-used stack is flagged hot');
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

test('the percent bar refuses a missing or empty stack', () => {
	assert.equal(renderPercentBar(512, null), '');
	assert.equal(renderPercentBar(null, 2048), '');
	assert.equal(renderPercentBar(10, 0), '');
});

test('renderAddress escapes the address text', () => {
	assert.ok(renderAddress('<script>0x0</script>').includes('&lt;script&gt;'));
});
