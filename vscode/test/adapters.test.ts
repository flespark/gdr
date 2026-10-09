/**
 * Unit tests for the adapter contract the manifest and the injection plan share.
 *
 * The defect these guard against: the supported-type list lived in
 * `extension.ts`, the injection keys lived in `inject.ts`, and `package.json`
 * named a third set. An adapter could therefore be activated for, subscribed
 * to, and injected into without those agreeing -- which is how every
 * `stlinkgdbtarget` session activated the extension and then received no
 * injection at all, because its launch key was not the one the plan wrote.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { test } from 'node:test';

import { REFUSED_DEBUG_TYPES, supportedDebugTypes, trackedDebugTypes } from '../src/inject.ts';

/** The extension manifest; the npm test script runs from the extension root. */
function activationEvents(): string[] {
	const manifest = JSON.parse(readFileSync(join(process.cwd(), 'package.json'), 'utf8')) as {
		activationEvents?: string[];
	};
	return manifest.activationEvents ?? [];
}

/** The debug types the manifest activates for. */
function activatedTypes(): string[] {
	return activationEvents()
		.filter((event) => event.startsWith('onDebugResolve:'))
		.map((event) => event.slice('onDebugResolve:'.length));
}

test('the activation events name exactly the types the tracker subscribes to', () => {
	assert.deepEqual(activatedTypes().sort(), [...trackedDebugTypes()].sort());
});

test('every injected type is activated, so its session can be injected at all', () => {
	// Reason: `registerDebugConfigurationProvider('*')` only runs once the
	// extension is active. A supported type with no activation event is a
	// session that starts, reaches the tracker, and never gets a loader.
	for (const type of supportedDebugTypes()) {
		assert.ok(activatedTypes().includes(type), `${type} has no onDebugResolve activation event`);
	}
});

test('every refused type is activated, so its session can be told why', () => {
	for (const type of REFUSED_DEBUG_TYPES) {
		assert.ok(activatedTypes().includes(type), `${type} has no onDebugResolve activation event`);
	}
});

test('a type is never both injected into and refused', () => {
	const supported = new Set(supportedDebugTypes());
	for (const type of REFUSED_DEBUG_TYPES) {
		assert.equal(supported.has(type), false, `${type} is supported and refused at once`);
	}
});

test('no type is listed twice', () => {
	const tracked = trackedDebugTypes();
	assert.equal(new Set(tracked).size, tracked.length);

	const activated = activatedTypes();
	assert.equal(new Set(activated).size, activated.length);
});
