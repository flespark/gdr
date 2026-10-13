/**
 * Unit tests for the webview message decoder: the only place the host decides
 * whether a click from the panel is a request it may act on.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { decodeLayout, decodeWebviewMessage } from '../src/webview/messages.ts';

test('an address click decodes to a memory request, keeping its session', () => {
	assert.deepEqual(decodeWebviewMessage({ type: 'openMemory', address: '0x20001234', sessionId: 's1' }), {
		type: 'openMemory',
		address: '0x20001234',
		sessionId: 's1'
	});
	assert.deepEqual(decodeWebviewMessage({ type: 'openMemory', address: '0x1' }), {
		type: 'openMemory',
		address: '0x1'
	});
});

test('a source click decodes only with an absolute file and a whole line', () => {
	assert.deepEqual(decodeWebviewMessage({ type: 'openSource', file: '/src/shell.c', name: 'shell.c', line: 1700 }), {
		type: 'openSource',
		file: '/src/shell.c',
		name: 'shell.c',
		line: 1700
	});
	assert.deepEqual(
		decodeWebviewMessage({ type: 'openSource', file: 'C:\\src\\shell.c', name: 'shell.c', line: 3, sessionId: 's2' }),
		{
			type: 'openSource',
			file: 'C:\\src\\shell.c',
			name: 'shell.c',
			line: 3,
			sessionId: 's2'
		}
	);
	// Reason: GDB reports fullnames; a relative path would open whatever file
	// the editor resolves it to, so it is refused.
	assert.equal(decodeWebviewMessage({ type: 'openSource', file: 'shell.c', name: 'shell.c', line: 3 }), null);
	assert.equal(decodeWebviewMessage({ type: 'openSource', file: '/src/shell.c', name: 'shell.c', line: 0 }), null);
	assert.equal(decodeWebviewMessage({ type: 'openSource', file: '/src/shell.c', name: 'shell.c', line: 1.5 }), null);
	assert.equal(decodeWebviewMessage({ type: 'openSource', file: '/src/shell.c', name: 'shell.c', line: '12' }), null);
});

test('a source click without a recorded name falls back to the file, and keeps its memory fallback', () => {
	assert.deepEqual(decodeWebviewMessage({ type: 'openSource', file: '/src/shell.c', line: 9 }), {
		type: 'openSource',
		file: '/src/shell.c',
		name: '/src/shell.c',
		line: 9
	});
	assert.deepEqual(
		decodeWebviewMessage({
			type: 'openSource',
			file: '/src/shell.c',
			name: 'shell.c',
			line: 9,
			fallbackAddress: '0x600bcc00'
		}),
		{
			type: 'openSource',
			file: '/src/shell.c',
			name: 'shell.c',
			line: 9,
			fallbackAddress: '0x600bcc00'
		}
	);
});

test('a heap-dump, refresh and layout message decode to their request shapes', () => {
	assert.deepEqual(decodeWebviewMessage({ type: 'heapDump', sessionId: 's3' }), { type: 'heapDump', sessionId: 's3' });
	assert.deepEqual(decodeWebviewMessage({ type: 'refresh' }), { type: 'refresh' });
});

test('a layout message keeps order, hidden tables and positive widths only', () => {
	const decoded = decodeWebviewMessage({
		type: 'layout',
		layout: {
			order: ['task', 'heap', 'semaphore'],
			hiddenTables: ['timer'],
			tables: {
				task: { widths: { Name: 140.4, PC: -3, Entry: Number.NaN, Addr: 80 }, hiddenColumns: ['Addr', 7] },
				semaphore: 'not a table'
			}
		}
	});
	assert.deepEqual(decoded, {
		type: 'layout',
		layout: {
			order: ['task', 'heap', 'semaphore'],
			hiddenTables: ['timer'],
			tables: {
				task: { kind: 'task', widths: { Name: 140, Addr: 80 }, hiddenColumns: ['Addr'] }
			}
		}
	});
	assert.equal(decodeWebviewMessage({ type: 'layout' }), null);
	assert.deepEqual(decodeLayout({ order: 'task' }), { order: [], hiddenTables: [], tables: {} });
});

test('anything else from the webview is dropped', () => {
	assert.equal(decodeWebviewMessage(null), null);
	assert.equal(decodeWebviewMessage('openMemory'), null);
	assert.equal(decodeWebviewMessage({ type: 'eval', expression: 'rm -rf /' }), null);
	assert.equal(decodeWebviewMessage({ type: 'openMemory' }), null);
});


test('column ordering is decoded, deduplicated and compatible with old layouts', () => {
 const layout = decodeLayout({ tables: { task: { columnOrder: ['PC', 'Name', 'PC', 1] } } });
 assert.deepEqual(layout?.tables.task.columnOrder, ['PC', 'Name']);
 assert.equal(decodeLayout({ tables: { task: {} } })?.tables.task.columnOrder, undefined);
});
