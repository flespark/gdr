/**
 * Unit tests for the address -> memory-view navigation.
 *
 * The URI follows the mcu-debug memory-view protocol (the shared viewer for
 * cortex-debug and siblings): an authority named after the extension, the
 * address as an `expr` query the viewer evaluates as a bare constant.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { MEMORY_VIEW_EXTENSION_ID, memoryViewMissingHint, memoryViewUri } from '../src/memoryNav.ts';

test('the URI names the memory-view extension and carries the address', () => {
	const uri = memoryViewUri('vscode', '0x20001234');
	assert.equal(uri, 'vscode://mcu-debug.memory-view?expr=0x20001234');
});

test('an explicit session binds the view to that debug session', () => {
	const uri = memoryViewUri('vscode', '0x20001234', { sessionId: 'sess-1', displayName: '0x20001234' });
	assert.equal(
		uri,
		'vscode://mcu-debug.memory-view?expr=0x20001234&sessionId=sess-1&displayName=0x20001234'
	);
});

test('it uses the caller scheme, so Insiders and VSCodium work too', () => {
	const uri = memoryViewUri('vscode-insiders', '0x20001234');
	assert.ok(uri.startsWith('vscode-insiders://mcu-debug.memory-view'));
});

test('the missing-extension hint names the exact id to install', () => {
	const hint = memoryViewMissingHint();
	assert.ok(hint.includes(MEMORY_VIEW_EXTENSION_ID));
});
