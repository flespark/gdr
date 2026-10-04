import assert from 'node:assert/strict';
import { test } from 'node:test';

import { describeValue, resolveEventSession, resolveTrackerApi } from '../src/tracker.ts';

const instance = { subscribe: () => 'granted' };

test('resolves the exports object when it is the shared tracker instance', async () => {
	const resolved = await resolveTrackerApi(instance);
	assert.equal(resolved, instance);
});

test('resolves through DebugTrackerFactory and passes the client name', async () => {
	const seen: string[] = [];
	const factory = {
		DebugTrackerFactory: {
			getTrackerExtension: async (name: string) => {
				seen.push(name);
				return instance;
			}
		}
	};
	const resolved = await resolveTrackerApi(factory);
	assert.equal(resolved, instance);
	assert.deepEqual(seen, ['gdb-rtos-views']);
});

test('passes a factory refusal through as the error', async () => {
	const refusal = new Error('no such debugger');
	const resolved = await resolveTrackerApi({
		DebugTrackerFactory: { getTrackerExtension: async () => refusal }
	});
	assert.equal(resolved, refusal);
});

test('reports a factory that answers with something that is not a tracker', async () => {
	const resolved = await resolveTrackerApi({
		DebugTrackerFactory: { getTrackerExtension: async () => ({ nope: 1 }) }
	});
	assert.ok(resolved instanceof Error);
	assert.match(resolved.message, /returned no tracker \(object with keys \[nope\]\)/);
});

test('reports a throwing factory instead of rejecting the activation', async () => {
	const resolved = await resolveTrackerApi({
		DebugTrackerFactory: {
			getTrackerExtension: async () => {
				throw new Error('boom');
			}
		}
	});
	assert.ok(resolved instanceof Error);
	assert.equal(resolved.message, 'boom');
});

test('names the exported shape when neither API is present', async () => {
	const resolved = await resolveTrackerApi({ something: 1 });
	assert.ok(resolved instanceof Error);
	assert.match(resolved.message, /neither a tracker instance nor DebugTrackerFactory/);
	assert.match(resolved.message, /object with keys \[something\]/);
});

test('describes scalars, arrays and empty objects', () => {
	assert.equal(describeValue(null), 'null');
	assert.equal(describeValue('x'), 'string');
	assert.equal(describeValue(7), 'number');
	assert.equal(describeValue([1]), 'array');
	assert.equal(describeValue({}), 'object with no keys');
	assert.equal(describeValue({ a: 1, b: 2 }), 'object with keys [a, b]');
});

test('caps the described key list', () => {
	const wide = Object.fromEntries(Array.from({ length: 12 }, (_unused, i) => [`k${i}`, i]));
	const listed = /\[([^\]]*)\]/.exec(describeValue(wide))?.[1].split(', ') ?? [];
	assert.equal(listed.length, 8);
});

test('attributes an event through its session id when no session is attached', () => {
	const live = { id: 's1' };
	const seen: string[] = [];
	const resolved = resolveEventSession({ sessionId: 's1' }, (id) => {
		seen.push(id);
		return id === 's1' ? live : undefined;
	});
	assert.equal(resolved, live);
	assert.deepEqual(seen, ['s1']);
});

test('prefers an attached session object over the id lookup', () => {
	const attached = { id: 's1' };
	let called = false;
	const resolved = resolveEventSession({ sessionId: 'other', session: attached }, () => {
		called = true;
		return { id: 'looked-up' };
	});
	assert.equal(resolved, attached);
	assert.equal(called, false);
});

test('drops an event whose session id is unknown', () => {
	assert.equal(
		resolveEventSession({ sessionId: 'gone' }, () => undefined),
		undefined
	);
});

test('drops an event that names no session at all', () => {
	assert.equal(
		resolveEventSession({}, () => ({ id: 's1' })),
		undefined
	);
});
