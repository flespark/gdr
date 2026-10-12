/**
 * Unit tests for the change highlight's baseline: first stop, unchanged values,
 * changed values, new rows, and that the keys match the rendered cells.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { changedKeys, cellValues } from '../src/diff.ts';
import type { ObjectTableData, PanelSnapshot, SnapshotData } from '../src/snapshot.ts';
import { changeKey, renderApp } from '../src/webview/render.ts';

function tasks(rows: string[][], ids: string[]): ObjectTableData {
	return {
		headers: ['Name', 'State', 'Prio', 'TCB'],
		rows,
		messages: [],
		elastic: ['Name'],
		columns: [
			{ key: 'Name', kind: 'text', align: 'left', elastic: true, doc: '', group: 'basic' },
			{ key: 'State', kind: 'text', align: 'left', elastic: false, doc: '', group: 'basic' },
			{ key: 'Prio', kind: 'int', align: 'right', elastic: false, doc: '', group: 'basic' },
			{ key: 'TCB', kind: 'addr', align: 'left', elastic: false, doc: '', group: 'basic' }
		],
		ids
	};
}

function snapshot(data: Partial<SnapshotData>): PanelSnapshot {
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
		data: {
			tasks: null,
			tables: null,
			system: null,
			heap: null,
			capabilities: null,
			...data
		}
	};
}

test('the first stop has no baseline, so nothing is highlighted', () => {
	const next = snapshot({ tasks: tasks([['main', 'running', '10', '0x2000']], ['0x2000']) });
	assert.equal(changedKeys(null, next).size, 0);
});

test('an unchanged value is not highlighted, and a changed one is', () => {
	const before = snapshot({
		tasks: tasks(
			[
				['main', 'running', '10', '0x2000'],
				['worker', 'suspend', '20', '0x2010']
			],
			['0x2000', '0x2010']
		)
	});
	const after = snapshot({
		tasks: tasks(
			[
				['main', 'suspend', '10', '0x2000'],
				['worker', 'suspend', '20', '0x2010']
			],
			['0x2000', '0x2010']
		)
	});
	const changed = changedKeys(before, after);
	assert.deepEqual([...changed], [changeKey('task', '0x2000', 'State')]);
});

test('a row that did not exist before is not highlighted: it has no baseline', () => {
	const before = snapshot({ tasks: tasks([['main', 'running', '10', '0x2000']], ['0x2000']) });
	const after = snapshot({
		tasks: tasks(
			[
				['main', 'running', '10', '0x2000'],
				['newcomer', 'ready', '5', '0x3000']
			],
			['0x2000', '0x3000']
		)
	});
	assert.equal(changedKeys(before, after).size, 0);
});

test('system and heap pairs are highlighted by their label', () => {
	const before = snapshot({
		system: { pairs: [['Tick count', '100'], ['Scheduler state', 'Unlocked']] },
		heap: { pairs: [['UsedSize', '10 B']], messages: [], sections: [] }
	});
	const after = snapshot({
		system: { pairs: [['Tick count', '101'], ['Scheduler state', 'Unlocked']] },
		heap: { pairs: [['UsedSize', '12 B']], messages: [], sections: [] }
	});
	const changed = changedKeys(before, after);
	assert.ok(changed.has(changeKey('system', '', 'Tick count')));
	assert.ok(changed.has(changeKey('heap', '', 'UsedSize')));
	assert.ok(!changed.has(changeKey('system', '', 'Scheduler state')));
});

test('a detail table cell is keyed by its kind, row id and column', () => {
	const detail: ObjectTableData = {
		headers: ['Name', 'Address', 'Value'],
		rows: [['sem', '0x2000', '3']],
		messages: [],
		elastic: ['Name'],
		ids: ['0x2000']
	};
	const values = cellValues(snapshot({ tables: { semaphore: detail } }));
	assert.equal(values.get(changeKey('semaphore', '0x2000', 'Value')), JSON.stringify(['3', null]));
});

test('the diff keys match the cells the renderer marks, so a highlight lands on its cell', () => {
	const before = snapshot({ tasks: tasks([['main', 'running', '10', '0x2000']], ['0x2000']) });
	const after = snapshot({ tasks: tasks([['main', 'running', '11', '0x2000']], ['0x2000']) });
	const changed = changedKeys(before, after);

	const html = renderApp(
		{
			sessions: [{ id: 's1', name: 'firmware', status: 'ok', message: null, snapshot: after }],
			activeId: 's1'
		},
		{ changed }
	);
	assert.ok(html.includes('<td class="gdr-right gdr-changed">11</td>'), 'the changed Prio is the marked cell');
	assert.ok(!html.includes('<td class="gdr-changed">running</td>'), 'an unchanged State is not marked');
});
