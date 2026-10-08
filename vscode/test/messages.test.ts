/**
 * Unit tests for the webview message decoder: the only place the host decides
 * whether a click from the panel is a request it may act on.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { decodeWebviewMessage } from '../src/webview/messages.ts';

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
	assert.deepEqual(decodeWebviewMessage({ type: 'openSource', file: '/src/shell.c', line: 1700 }), {
		type: 'openSource',
		file: '/src/shell.c',
		line: 1700
	});
	assert.deepEqual(decodeWebviewMessage({ type: 'openSource', file: 'C:\\src\\shell.c', line: 3, sessionId: 's2' }), {
		type: 'openSource',
		file: 'C:\\src\\shell.c',
		line: 3,
		sessionId: 's2'
	});
	// Reason: GDB reports fullnames; a relative path would open whatever file
	// the editor resolves it to, so it is refused.
	assert.equal(decodeWebviewMessage({ type: 'openSource', file: 'shell.c', line: 3 }), null);
	assert.equal(decodeWebviewMessage({ type: 'openSource', file: '/src/shell.c', line: 0 }), null);
	assert.equal(decodeWebviewMessage({ type: 'openSource', file: '/src/shell.c', line: 1.5 }), null);
	assert.equal(decodeWebviewMessage({ type: 'openSource', file: '/src/shell.c', line: '12' }), null);
});

test('anything else from the webview is dropped', () => {
	assert.equal(decodeWebviewMessage(null), null);
	assert.equal(decodeWebviewMessage('openMemory'), null);
	assert.equal(decodeWebviewMessage({ type: 'eval', expression: 'rm -rf /' }), null);
	assert.equal(decodeWebviewMessage({ type: 'openMemory' }), null);
});
