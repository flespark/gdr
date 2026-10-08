/**
 * Unit tests for the snapshot envelope parsing, the cost arguments and the
 * freshness guard. All pure: no debug session, no webview.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { costArgs, isStale, parseSnapshot, parseSnapshotTransport } from '../src/snapshot.ts';
import type { GdrEnvelope } from '../src/transport.ts';

function envelope(data: unknown, generation?: number): GdrEnvelope {
	return { schema_version: 1, ok: true, data, ...(generation !== undefined ? { generation } : {}) };
}

const TABLE = {
	headers: ['Name', 'Prio', 'Addr'],
	rows: [
		['main *', '10', '0x20001234'],
		['worker', '20', '0x20001568']
	],
	messages: ['scan note'],
	elastic: ['Name'],
	kind: 'task',
	columns: [
		{ key: 'Name', kind: 'text', align: 'left', elastic: true, doc: 'thread name' },
		{ key: 'Prio', kind: 'int', align: 'right', elastic: false, doc: '' },
		{ key: 'Addr', kind: 'addr', align: 'left', elastic: false, doc: '' }
	],
	value_rows: [
		['main *', 10, 0x20001234],
		['worker', 20, 0x20001568]
	],
	ids: ['0x20001234', '0x20001568']
};

function snapshotPayload(): unknown {
	return {
		tasks: TABLE,
		objects: { headers: ['Kind', 'Count'], rows: [['semaphore', '3']], messages: [], elastic: [] },
		system: { pairs: [['Kernel version', '4.0.5'], ['Current task', 'main']] },
		heap: { pairs: [['Algorithm', 'small']], messages: [], table: null, tableTitle: '' },
		capabilities: {
			kinds: {
				semaphore: {
					enumerable: true,
					detail: '',
					columns: ['Name', 'Value'],
					note: ''
				},
				queue: { enumerable: false, detail: 'needs configQUEUE_REGISTRY_SIZE', columns: [], note: '' }
			},
			extra: ['nothing extra']
		},
		checks: { findings: [{ name: 'TaskCount', verdict: 'ok', detail: '', scope: 'system' }], counts: { ok: 1 } }
	};
}

test('a snapshot parse keeps the machine values and stable ids', () => {
	const data = must(parseSnapshot(snapshotPayload()), 'the fixture must parse');
	assert.ok(data.tasks);
	assert.deepEqual(data.tasks.rows[0], ['main *', '10', '0x20001234']);
	assert.deepEqual(data.tasks.value_rows?.[0], ['main *', 10, 0x20001234]);
	assert.deepEqual(data.tasks.ids, ['0x20001234', '0x20001568']);
	assert.equal(data.tasks.columns?.[1].align, 'right');
	assert.deepEqual(data.system?.pairs[0], ['Kernel version', '4.0.5']);
	assert.equal(data.capabilities?.kinds.queue.detail, 'needs configQUEUE_REGISTRY_SIZE');
	assert.equal(data.checks?.counts.ok, 1);
});

test('a source location parses only with a file and a whole line number', () => {
	const data = parseSnapshot({
		tasks: {
			headers: ['Name', 'Entry'],
			rows: [
				['tshell', '<finsh_thread_entry>'],
				['worker', '<worker_entry>']
			],
			messages: [],
			elastic: [],
			source_rows: [
				[null, { file: '/src/shell.c', line: 1700 }],
				[null, { file: '/src/worker.c', line: 1.5 }]
			]
		}
	});
	assert.deepEqual(data?.tasks?.source_rows, [
		[null, { file: '/src/shell.c', line: 1700 }],
		[null, null]
	]);
});

test('a non-snapshot envelope payload parses as null', () => {
	assert.equal(parseSnapshot({ candidates: [] }), null);
	assert.equal(parseSnapshot('tasks'), null);
	assert.equal(parseSnapshot(null), null);
});

test('the no-heap cost option becomes the snapshot argument', () => {
	assert.deepEqual(costArgs({ noHeap: false }), []);
	assert.deepEqual(costArgs({ noHeap: true }), ['--no-heap']);
});

test('the transport setting decodes to a domain value, defaulting to evaluate', () => {
	assert.equal(parseSnapshotTransport('file'), 'file');
	assert.equal(parseSnapshotTransport('evaluate'), 'evaluate');
	assert.equal(parseSnapshotTransport(undefined), 'evaluate');
	assert.equal(parseSnapshotTransport(true), 'evaluate', 'a stale boolean value must not select the file path');
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
