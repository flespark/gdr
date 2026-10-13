/**
 * Unit tests for the two-level checkbox tree: the greying rules, the basic and
 * diagnostic child groups, the all-ticked default, and the layout a tick makes.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { PanelSnapshot, SnapshotData } from '../src/snapshot.ts';
import { buildTableTree, toggleColumn, toggleTable } from '../src/webview/checkboxes.ts';
import type { PanelLayout } from '../src/webview/messages.ts';
import { renderCheckboxTree } from '../src/webview/render.ts';

function snapshot(overrides: Partial<SnapshotData> = {}, flags: Partial<PanelSnapshot> = {}): PanelSnapshot {
	const data: SnapshotData = {
		tasks: {
			headers: ['Name', 'Prio', 'PC', 'Addr'],
			rows: [['main', '10', '<rt_schedule+584>', '0x2000']],
			messages: [],
			elastic: ['Name'],
			columns: [
				{ key: 'Name', kind: 'text', align: 'left', elastic: true, doc: '', group: 'basic' },
				{ key: 'Prio', kind: 'int', align: 'right', elastic: false, doc: '', group: 'basic' },
				{ key: 'PC', kind: 'pc', align: 'left', elastic: false, doc: '', group: 'basic' },
				{ key: 'Addr', kind: 'addr', align: 'left', elastic: false, doc: '', group: 'basic' }
			]
		},
		tables: {
			semaphore: {
				headers: ['Name', 'Value', 'OffsetCheck'],
				rows: [['sem', '1', 'ok']],
				messages: [],
				elastic: ['Name'],
				columns: [
					{ key: 'Name', kind: 'text', align: 'left', elastic: true, doc: '', group: 'basic' },
					{ key: 'Value', kind: 'text', align: 'left', elastic: false, doc: '', group: 'basic' },
					{ key: 'OffsetCheck', kind: 'text', align: 'left', elastic: false, doc: '', group: 'diag' }
				]
			}
		},
		system: null,
		heap: {
			pairs: [['Algorithm', 'small_mem']],
			messages: [],
			sections: [
				{
					title: 'Checks',
					rows: [
						{ key: 'HeapChain', value: 'ok', severity: 'ok' },
						{ key: 'HeapAccounting', value: 'ok', severity: 'ok' }
					]
				}
			]
		},
		capabilities: {
			kinds: {
				mutex: { enumerable: false, detail: 'mutex kind is disabled in this kernel', columns: [], note: '' }
			},
			extra: []
		},
		...overrides
	};
	return {
		rtos: 'rtthread',
		rtosVersion: '4.0.5',
		gdrVersion: '2026.3',
		generation: 1,
		elapsedMs: 1,
		collectedAt: 0,
		warnings: [],
		noHeap: false,
		noDetails: false,
		data,
		...flags
	};
}

function nodeOf(tree: ReturnType<typeof buildTableTree>, kind: string) {
	const node = tree.find((candidate) => candidate.kind === kind);
	assert.ok(node, `the tree has a ${kind} parent`);
	return node;
}

test('every table kind is a parent, in the default order, all ticked with no layout', () => {
	const tree = buildTableTree(snapshot(), undefined);
	assert.deepEqual(
		tree.map((node) => node.kind),
		['task', 'timer', 'heap', 'semaphore', 'mutex', 'event', 'mailbox', 'msgqueue', 'mempool', 'queue', 'eventgroup', 'streambuffer']
	);
	assert.ok(tree.every((node) => node.checked === true), 'the first launch shows every table');
});

test('a heap with its switch off is greyed out, with the switch as the reason', () => {
	const heapOff = snapshot({ heap: null }, { noHeap: true });
	const node = nodeOf(buildTableTree(heapOff, undefined), 'heap');
	assert.equal(node.available, false);
	assert.equal(node.reason, 'heap collection is off');
});

test('a heap the target does not have is greyed out for that reason', () => {
	const noHeap = snapshot({ heap: null }, { noHeap: false });
	const node = nodeOf(buildTableTree(noHeap, undefined), 'heap');
	assert.equal(node.available, false);
	assert.equal(node.reason, 'the target has no heap');
});

test('a kind the target cannot enumerate is greyed out with its capability reason', () => {
	const node = nodeOf(buildTableTree(snapshot(), undefined), 'mutex');
	assert.equal(node.available, false);
	assert.equal(node.reason, 'mutex kind is disabled in this kernel');
});

test('a kind with no table because detail tables are off says so', () => {
	const node = nodeOf(buildTableTree(snapshot({ tables: null }, { noDetails: true }), undefined), 'semaphore');
	assert.equal(node.available, false);
	assert.equal(node.reason, 'detail tables are off');
});

test('children split into basic and diagnostic columns by the adapter contract', () => {
	const node = nodeOf(buildTableTree(snapshot(), undefined), 'semaphore');
	assert.deepEqual(
		node.basic.map((column) => column.key),
		['Name', 'Value']
	);
	assert.deepEqual(
		node.diag.map((column) => column.key),
		['OffsetCheck']
	);
	assert.ok(node.diag.every((column) => column.group === 'diag'));
});

test('the heap children are its verdict checks, all diagnostic and ticked by default', () => {
	const node = nodeOf(buildTableTree(snapshot(), undefined), 'heap');
	assert.deepEqual(
		node.diag.map((column) => column.key),
		['HeapChain', 'HeapAccounting']
	);
	assert.ok(node.diag.every((column) => column.checked));
});

test('a saved layout hides tables and columns, and the tree shows the same state', () => {
	const layout: PanelLayout = {
		order: ['semaphore'],
		hiddenTables: ['timer'],
		tables: { semaphore: { kind: 'semaphore', widths: {}, hiddenColumns: ['OffsetCheck'] } }
	};
	const tree = buildTableTree(snapshot(), layout);
	assert.equal(tree[0].kind, 'semaphore', 'the saved order leads the tree too');
	assert.equal(nodeOf(tree, 'timer').checked, false);
	const semaphore = nodeOf(tree, 'semaphore');
	assert.equal(semaphore.diag[0].checked, false);
	assert.equal(semaphore.basic[0].checked, true);
});

test('a greyed parent is never drawn ticked, even when the layout says shown', () => {
	const layout: PanelLayout = { order: [], hiddenTables: [], tables: {} };
	const markup = renderCheckboxTree(buildTableTree(snapshot({ heap: null }, { noHeap: true }), layout));
	assert.ok(markup.includes('data-table="heap" disabled/>'), 'the heap box is disabled');
	assert.ok(!markup.includes('data-table="heap" checked'), 'and not ticked');
	assert.ok(markup.includes('gdr-tree-reason'), 'the reason is shown beside it');
});

test('the tree markup carries the table and column each box controls', () => {
	const markup = renderCheckboxTree(buildTableTree(snapshot(), undefined));
	assert.ok(markup.includes('data-table="semaphore" checked'));
	assert.ok(markup.includes('data-table="semaphore" data-column="OffsetCheck" checked'));
	assert.ok(markup.includes('gdr-tree-diag'), 'diagnostic children are marked');
	assert.ok(!/\sstyle=/.test(markup), 'no inline styles');
});

test('toggling a table or a column yields the next layout without touching the rest', () => {
	const start: PanelLayout = {
		order: ['task'],
		hiddenTables: ['heap'],
		tables: { task: { kind: 'task', widths: { Name: 120 }, hiddenColumns: [] } }
	};
	const hidden = toggleTable(start, 'timer', false);
	assert.deepEqual(hidden.hiddenTables, ['heap', 'timer']);
	const shown = toggleTable(hidden, 'heap', true);
	assert.deepEqual(shown.hiddenTables, ['timer']);

	const column = toggleColumn(start, 'task', 'Addr', false);
	assert.deepEqual(column.tables.task, { kind: 'task', widths: { Name: 120 }, hiddenColumns: ['Addr'] });
	const back = toggleColumn(column, 'task', 'Addr', true);
	assert.deepEqual(back.tables.task.hiddenColumns, []);
	assert.deepEqual(back.order, ['task'], 'the order is untouched');
});


test('checkbox order follows table order and interleaved diagnostic column order', () => {
 const layout: PanelLayout = { order: ['semaphore', 'task', 'semaphore'], hiddenTables: [], tables: { semaphore: { kind: 'semaphore', widths: {}, hiddenColumns: ['Value'], columnOrder: ['OffsetCheck', 'Name', 'Value'] } } };
 const tree = buildTableTree(snapshot(), layout);
 assert.deepEqual(tree.slice(0, 2).map((node) => node.kind), ['semaphore', 'task']);
 assert.equal(tree.filter((node) => node.kind === 'semaphore').length, 1);
 assert.deepEqual(nodeOf(tree, 'semaphore').columns.map((node) => node.key), ['OffsetCheck', 'Name', 'Value']);
 const html = renderCheckboxTree(tree);
 assert.ok(html.indexOf('data-column="OffsetCheck"') < html.indexOf('data-column="Name"'));
});

test('heap checkbox order follows its basic table then its diagnostic table', () => {
 const heap = nodeOf(buildTableTree(snapshot(), undefined), 'heap');
 assert.deepEqual(heap.columns.map((node) => node.key), ['Algorithm', 'HeapChain', 'HeapAccounting']);
});
