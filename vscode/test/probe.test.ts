/**
 * Unit tests for the probe decision rules and the session cache key.
 *
 * The rules exist so that a version is never guessed: GDR reports evidence, and
 * the plugin only initializes automatically when exactly one
 * version is unambiguous.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { cacheKey, decideInit, evidenceLines, parseProbePayload } from '../src/probe.ts';

const SETTINGS = { rtos: '', version: '', autoInit: true };

function payload(candidates: unknown[], resolved: unknown = null): unknown {
	return { candidates, resolved };
}

test('a payload is parsed with its evidence, ignoring malformed rows', () => {
	const parsed = parseProbePayload(
		payload(
			[
				{
					rtos: 'rtthread',
					version: '4.0.5',
					confidence: 'exact',
					evidence: ['symbol: rt_thread_self', 7],
					detail: ''
				},
				{ version: '1.0.0' }
			],
			{ rtos: 'rtthread', version: '4.0.5' }
		)
	);

	assert.ok(parsed);
	assert.equal(parsed.candidates.length, 1);
	assert.deepEqual(parsed.candidates[0].evidence, ['symbol: rt_thread_self']);
	assert.deepEqual(parsed.resolved, { rtos: 'rtthread', version: '4.0.5' });
	assert.equal(parseProbePayload({ candidates: 'nope' }), null);
});

test('explicit settings win over the probe', () => {
	const decision = decideInit(
		parseProbePayload(payload([{ rtos: 'rtthread', version: '4.0.5', confidence: 'exact' }])),
		{ rtos: 'freertos', version: '10.3.1', autoInit: true }
	);

	assert.deepEqual(decision, { kind: 'init', rtos: 'freertos', version: '10.3.1', source: 'settings' });
});

test('a cached answer skips the probe round trip', () => {
	const decision = decideInit(null, SETTINGS, { rtos: 'rtthread', version: '4.1.1' });

	assert.deepEqual(decision, { kind: 'init', rtos: 'rtthread', version: '4.1.1', source: 'cache' });
});

test('an unambiguous exact probe initializes automatically', () => {
	const parsed = parseProbePayload(
		payload([{ rtos: 'rtthread', version: '4.0.5', confidence: 'exact' }], {
			rtos: 'rtthread',
			version: '4.0.5'
		})
	);

	assert.deepEqual(decideInit(parsed, SETTINGS), {
		kind: 'init',
		rtos: 'rtthread',
		version: '4.0.5',
		source: 'probe'
	});
});

test('autoInit disabled turns even an exact probe into a question', () => {
	const parsed = parseProbePayload(
		payload([{ rtos: 'rtthread', version: '4.0.5', confidence: 'exact' }], {
			rtos: 'rtthread',
			version: '4.0.5'
		})
	);

	const decision = decideInit(parsed, { ...SETTINGS, autoInit: false });

	assert.equal(decision.kind, 'ask');
	if (decision.kind === 'ask') {
		assert.match(decision.reason, /gdr\.autoInit/);
		assert.deepEqual(decision.options, [{ rtos: 'rtthread', version: '4.0.5' }]);
	}
});

test('a versionless or ambiguous probe becomes a question with the evidence', () => {
	const range = parseProbePayload(
		payload([
			{
				rtos: 'freertos',
				version: null,
				confidence: 'range',
				evidence: ['symbol: pxCurrentTCB', 'layout: xTaskRunState absent']
			}
		])
	);

	const decision = decideInit(range, SETTINGS);

	assert.equal(decision.kind, 'ask');
	if (decision.kind === 'ask') {
		assert.deepEqual(decision.options, []);
		assert.deepEqual(decision.evidence, [
			'freertos: symbol: pxCurrentTCB',
			'freertos: layout: xTaskRunState absent'
		]);
		assert.match(decision.reason, /no version could be read/);
	}

	const twoAdapters = parseProbePayload(
		payload([
			{ rtos: 'rtthread', version: '4.0.5', confidence: 'exact' },
			{ rtos: 'freertos', version: '10.3.1', confidence: 'exact' }
		])
	);
	assert.equal(decideInit(twoAdapters, SETTINGS).kind, 'ask');
});

test('nothing detected is reported as none, never as a default', () => {
	assert.deepEqual(decideInit(null, SETTINGS), {
		kind: 'none',
		reason: 'the probe returned no readable payload'
	});
	assert.deepEqual(decideInit(parseProbePayload(payload([])), SETTINGS), {
		kind: 'none',
		reason: 'no supported RTOS was detected on this target'
	});
});

test('a pinned RTOS without a version asks only about that adapter', () => {
	const parsed = parseProbePayload(
		payload([
			{ rtos: 'rtthread', version: '4.0.5', confidence: 'exact' },
			{ rtos: 'freertos', version: '10.3.1', confidence: 'exact' }
		])
	);

	const decision = decideInit(parsed, { ...SETTINGS, rtos: 'rtthread' });

	assert.equal(decision.kind, 'ask');
	if (decision.kind === 'ask') {
		assert.deepEqual(decision.options, [{ rtos: 'rtthread', version: '4.0.5' }]);
	}
});

test('the cache key follows the firmware path, not just the session name', () => {
	// The two spellings the supported adapters declare.
	assert.equal(
		cacheKey({ type: 'cortex-debug', name: 'Debug', executable: '/tmp/a.elf' }),
		'cortex-debug|Debug|/tmp/a.elf'
	);
	assert.equal(cacheKey({ type: 'cppdbg', name: 'D', program: '/tmp/b.elf' }), 'cppdbg|D|/tmp/b.elf');

	// No readable firmware path means no cache, never a key on the name alone.
	assert.equal(cacheKey({ type: 'cppdbg', name: 'D' }), null);
	assert.equal(cacheKey({ type: 'cortex-debug', name: 'D', executable: '' }), null);
	assert.equal(cacheKey(undefined), null);
});

test('evidence lines name the adapter they came from', () => {
	const parsed = parseProbePayload(
		payload([{ rtos: 'rtthread', confidence: 'exact', evidence: ['symbol: rt_tick'] }])
	);

	assert.ok(parsed);
	assert.deepEqual(evidenceLines(parsed), ['rtthread: symbol: rt_tick']);
});
