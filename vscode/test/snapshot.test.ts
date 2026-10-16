/**
 * Unit tests for the snapshot envelope parsing, the cost arguments and the
 * freshness guard. All pure: no debug session, no webview.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	costArgs,
	isStale,
	parseHeapMap,
	parseSnapshot,
	parseSnapshotTransport
} from '../src/snapshot.ts';
import type { GdrEnvelope } from '../src/transport.ts';

function envelope(data: unknown, generation?: number): GdrEnvelope {
	return { schema_version: 2, ok: true, data, ...(generation !== undefined ? { generation } : {}) };
}

const TASKS = {
	headers: ['Name', 'Prio', 'PC', 'Addr'],
	rows: [
		['main *', '10', '<rt_schedule+584>', '0x20001234'],
		['worker', '20', '<rt_thread_idle_entry>', '0x20001568']
	],
	messages: [],
	elastic: ['Name'],
	kind: 'task',
	columns: [
		{ key: 'Name', kind: 'text', align: 'left', elastic: true, doc: 'thread name', group: 'basic' },
		{ key: 'Prio', kind: 'int', align: 'right', elastic: false, doc: '', group: 'basic' },
		{ key: 'PC', kind: 'pc', align: 'left', elastic: false, doc: 'last PC', group: 'basic' },
		{ key: 'Addr', kind: 'addr', align: 'left', elastic: false, doc: '', group: 'basic' }
	],
	value_rows: [
		['main *', 10, '<rt_schedule+584>', 0x20001234],
		['worker', 20, '<rt_thread_idle_entry>', 0x20001568]
	],
	ids: ['0x20001234', '0x20001568']
};

const SEMAPHORE_TABLE = {
	headers: ['Name', 'Address', 'Value', 'OffsetCheck'],
	rows: [['sem', '0x2000', '1', 'fail: in_offset >= size']],
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
};

function snapshotPayload(): unknown {
	return {
		tasks: TASKS,
		tables: { semaphore: SEMAPHORE_TABLE },
		system: { pairs: [['Current task', 'main'], ['Scheduler state', 'Unlocked']] },
		heap: {
			pairs: [['Algorithm', 'small_mem']],
			messages: [],
			sections: [
				{
					title: 'Checks',
					rows: [{ key: 'HeapChain', value: 'ok', severity: 'ok' }]
				}
			]
		},
		capabilities: {
			kinds: {
				semaphore: { enumerable: true, detail: '', columns: ['Name', 'Value'], note: '' },
				queue: { enumerable: false, detail: 'needs configQUEUE_REGISTRY_SIZE', columns: [], note: '' }
			},
			extra: ['nothing extra']
		}
	};
}

test('a snapshot parse keeps the machine values, stable ids and column groups', () => {
	const data = must(parseSnapshot(snapshotPayload()), 'the fixture must parse');
	assert.ok(data.tasks);
	assert.deepEqual(data.tasks.rows[0], ['main *', '10', '<rt_schedule+584>', '0x20001234']);
	assert.deepEqual(data.tasks.value_rows?.[0], ['main *', 10, '<rt_schedule+584>', 0x20001234]);
	assert.deepEqual(data.tasks.ids, ['0x20001234', '0x20001568']);
	assert.equal(data.tasks.columns?.[1].align, 'right');
	assert.equal(data.tasks.columns?.[2].kind, 'pc');
	assert.equal(data.tasks.columns?.[0].group, 'basic');
	assert.equal(data.system?.pairs[0][0], 'Current task', 'the system pairs keep the Python order');
	assert.equal(data.capabilities?.kinds.queue.detail, 'needs configQUEUE_REGISTRY_SIZE');
});

test('the snapshot carries per-kind detail tables, with diagnostic columns grouped', () => {
	const data = must(parseSnapshot(snapshotPayload()), 'the fixture must parse');
	const semaphore = data.tables?.semaphore;
	assert.ok(semaphore);
	assert.equal(semaphore.rows[0][2], '1');
	assert.equal(semaphore.columns?.[3].group, 'diag');
	assert.equal(semaphore.columns?.[2].group, 'basic');
});

test('a snapshot without the checks section still parses, and the verdicts ride in heap', () => {
	const data = must(parseSnapshot(snapshotPayload()), 'the fixture must parse');
	assert.equal(data.heap?.sections[0].title, 'Checks');
	assert.deepEqual(data.heap?.sections[0].rows, [{ key: 'HeapChain', value: 'ok', severity: 'ok' }]);
	assert.ok(!('checks' in data), 'the checks section is gone from the snapshot');
});

test('a skipped detail set parses as null, not as an empty set', () => {
	const payload = { ...(snapshotPayload() as Record<string, unknown>), tables: null };
	const data = must(parseSnapshot(payload), 'the fixture must parse');
	assert.equal(data.tables, null);
});

test('a source location keeps the recorded name, falling back to the file', () => {
	const data = parseSnapshot({
		tasks: {
			headers: ['Name', 'PC'],
			rows: [
				['tshell', '<finsh_thread_entry>'],
				['worker', '<worker_entry>'],
				['bad', '<bad>']
			],
			messages: [],
			elastic: [],
			source_rows: [
				[null, { file: '/build/shell.c', name: 'applications/shell.c', line: 1700 }],
				[null, { file: '/src/worker.c', line: 1.5 }],
				[null, { file: '/src/old.c', line: 9 }]
			]
		}
	});
	assert.deepEqual(data?.tasks?.source_rows, [
		[null, { file: '/build/shell.c', name: 'applications/shell.c', line: 1700 }],
		[null, null],
		[null, { file: '/src/old.c', name: '/src/old.c', line: 9 }]
	]);
});

test('a non-snapshot envelope payload parses as null', () => {
	assert.equal(parseSnapshot({ candidates: [] }), null);
	assert.equal(parseSnapshot('tasks'), null);
	assert.equal(parseSnapshot(null), null);
});

test('the cost options become the snapshot arguments', () => {
	assert.deepEqual(costArgs({ noHeap: false, noDetails: false }), []);
	assert.deepEqual(costArgs({ noHeap: true, noDetails: false }), ['--no-heap']);
	assert.deepEqual(costArgs({ noHeap: false, noDetails: true }), ['--no-details']);
	assert.deepEqual(costArgs({ noHeap: true, noDetails: true }), ['--no-heap', '--no-details']);
});

test('the transport setting decodes to a domain value, defaulting to evaluate', () => {
	assert.equal(parseSnapshotTransport('file'), 'file');
	assert.equal(parseSnapshotTransport('evaluate'), 'evaluate');
	assert.equal(parseSnapshotTransport(undefined), 'evaluate');
	assert.equal(parseSnapshotTransport(true), 'evaluate', 'a stale boolean value must not select the file path');
});

test('a heap-dump envelope parses its blocks and palette for the canvas and the hover', () => {
	const map = parseHeapMap({
		algorithm: 'small_mem',
		base: 0x1000,
		end: 0x1100,
		owners: true,
		truncated: false,
		messages: [],
		blocks: [
			[0x1000, 0x40, true, 'main'],
			[0x1040, 0xc0, false, null],
			['bad', 1, true, null],
			[0x1100, 0x10, true, null, 'extra']
		],
		palette: { owners: { main: '#e15e3a' }, free: '#c8c8c8', unknown: '#4f8fd6' }
	});
	assert.ok(map);
	assert.deepEqual(map.blocks, [
		[0x1000, 0x40, true, 'main'],
		[0x1040, 0xc0, false, null]
	]);
	assert.equal(map.palette.owners.main, '#e15e3a');
	assert.equal(map.owners, true);
});

test('a heap-dump envelope without a palette is not a heap map', () => {
	assert.equal(parseHeapMap({ algorithm: 'small_mem', base: 0, end: 1, blocks: [] }), null);
	assert.equal(parseHeapMap('blocks'), null);
});

test('a response for an older stop is stale, a newer one is fresh', () => {
	assert.equal(isStale(null, envelope(snapshotPayload(), 5)), false, 'no baseline yet');
	assert.equal(isStale(5, envelope(snapshotPayload(), 5)), true, 'same stop must not reapply');
	assert.equal(isStale(5, envelope(snapshotPayload(), 6)), false, 'a newer stop is fresh');
	assert.equal(isStale(5, envelope(snapshotPayload())), false, 'a runtime without generation never goes stale');
});

function must<T>(value: T | null, message: string): T {
	if (value === null) {
		throw new Error(message);
	}
	return value;
}


test('heap grid runs must cover all cells and reference the unchanged block list', () => {
 const payload = { algorithm: 'small', base: 0, end: 100, blocks: [[0, 100, true, 'main']], palette: { owners: { main: '#e15e3a' }, free: '#c8c8c8', unknown: '#4f8fd6' }, grid: { width: 2, height: 1, scale: 6, runs: [[0, 2, 0]] } };
 assert.deepEqual(parseHeapMap(payload)?.grid, payload.grid);
 assert.equal(parseHeapMap({ ...payload, grid: { ...payload.grid, runs: [[0, 1, 0]] } }), null);
 assert.equal(parseHeapMap({ ...payload, grid: { ...payload.grid, runs: [[0, 2, 5]] } }), null);
 // Reason: the CLI cell size rides the same field from 1 to 64 pixels.
 assert.notEqual(parseHeapMap({ ...payload, grid: { ...payload.grid, scale: 3 } }), null);
 assert.notEqual(parseHeapMap({ ...payload, grid: { ...payload.grid, scale: 64 } }), null);
 assert.equal(parseHeapMap({ ...payload, grid: { ...payload.grid, scale: 0 } }), null);
 assert.equal(parseHeapMap({ ...payload, grid: { ...payload.grid, scale: 65 } }), null);
});

test('heap grid dividers ride along with heights, malformed ones reject the map', () => {
 const grid = {
  width: 2, height: 2, scale: 6, runs: [[0, 4, 0]],
  folds: [{ row: 1, height: 3, bytes: 500, kind: 'free', start: 64, end: 564 }],
  ticks: [{ unit: 0, addr: 0, major: true }]
 };
 const payload = { algorithm: 'small', base: 0, end: 700, blocks: [[0, 100, true, 'main']], palette: { owners: { main: '#e15e3a' }, free: '#c8c8c8', unknown: '#4f8fd6' }, grid };
 assert.deepEqual(parseHeapMap(payload)?.grid, { width: 2, height: 2, scale: 6, runs: [[0, 4, 0]], folds: [{ ...grid.folds[0], rows: 1 }] });
 assert.equal(parseHeapMap({ ...payload, grid: { ...grid, folds: [{ ...grid.folds[0], row: 5 }] } }), null);
 assert.equal(parseHeapMap({ ...payload, grid: { ...grid, folds: [{ ...grid.folds[0], height: 0 }] } }), null);
 assert.equal(parseHeapMap({ ...payload, grid: { ...grid, folds: 'free' } }), null);
 assert.deepEqual(parseHeapMap({ ...payload, grid: { ...grid, folds: [{ ...grid.folds[0], rows: 1 }] } })?.grid?.folds, [{ ...grid.folds[0], rows: 1 }]);
 assert.equal(parseHeapMap({ ...payload, grid: { ...grid, folds: [{ ...grid.folds[0], rows: 0 }] } }), null);
 assert.equal(parseHeapMap({ ...payload, grid: { ...grid, folds: [{ ...grid.folds[0], rows: 2 }] } }), null, 'a band covering past the grid is rejected');
 assert.equal(parseHeapMap({ ...payload, grid: { ...grid, folds: [{ ...grid.folds[0], rows: 1 }, { ...grid.folds[0], rows: 1 }] } }), null, 'overlapping bands are rejected');
});
