/**
 * Unit tests for the GDR transport: command spelling, envelope parsing and
 * chunk reassembly. All pure, so no debug session is needed.
 */

import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';

import { chunkExpression, describeFailure, evaluateExpression, parseChunkMeta, parseEnvelope, reassembleChunks, transportPlan } from '../src/transport.ts';

/**
 * Narrow a value the parser may refuse.
 *
 * `assert.ok` does not narrow for the type checker here, and the tests below
 * are specifically about the parser returning `null` on purpose, so the happy
 * path states its expectation explicitly.
 */
function must<T>(value: T | null, message: string): T {
	if (value === null) {
		throw new Error(message);
	}
	return value;
}

function chunkFrames(payload: string, chunkSize = 8): string[] {
	const encoded = Buffer.from(payload, 'utf8').toString('base64');
	const slices: string[] = [];
	for (let index = 0; index < encoded.length; index += chunkSize) {
		slices.push(encoded.slice(index, index + chunkSize));
	}
	const meta = {
		kind: 'chunks',
		schema_version: 1,
		total: slices.length,
		chunk_size: chunkSize,
		sha256: createHash('sha256').update(payload, 'utf8').digest('hex')
	};
	return [JSON.stringify(meta), ...slices];
}

test('an expression quotes a name that carries a quote or backslash', () => {
	assert.equal(evaluateExpression('tasks'), '$gdr_json("tasks")');
	assert.equal(
		evaluateExpression('detail', 'semaphore', 'we"ird\\name'),
		'$gdr_json("detail", "semaphore", "we\\"ird\\\\name")'
	);
	assert.equal(chunkExpression(3, 'tasks'), '$gdr_json_chunk(3, "tasks")');
});

test('an envelope is parsed and its optional fields narrowed', () => {
	const envelope = must(parseEnvelope(JSON.stringify({
		schema_version: 1,
		ok: false,
		data: null,
		error: { code: 'not_initialized', message: 'init first' },
		warnings: ['a note', 42],
		rtos: null,
		generation: 7
	})), 'the envelope must parse');

	assert.equal(envelope.ok, false);
	assert.equal(envelope.error?.code, 'not_initialized');
	assert.deepEqual(envelope.warnings, ['a note']);
	assert.equal(envelope.generation, 7);
	assert.equal(describeFailure(envelope), 'not_initialized: init first');
});

test('a payload that is not an envelope is rejected rather than guessed at', () => {
	assert.equal(parseEnvelope('not json'), null);
	assert.equal(parseEnvelope('{"ok": true}'), null);
	assert.equal(parseEnvelope(JSON.stringify({ schema_version: 1, ok: 'yes' })), null);
});

test('a chunked transfer reassembles to the exact payload', () => {
	const payload = JSON.stringify({ schema_version: 1, ok: true, data: { rows: ['x'.repeat(40)] } });
	const [metaFrame, ...slices] = chunkFrames(payload);
	const meta = must(parseChunkMeta(metaFrame), 'the meta frame must parse');

	assert.equal(meta.total, slices.length);
	assert.equal(reassembleChunks(meta, slices), payload);

	const plan = transportPlan(metaFrame);
	assert.equal(plan.kind, 'chunked');
});

test('a spliced transfer fails instead of rendering mixed data', () => {
	const [metaFrame, ...slices] = chunkFrames('{"schema_version": 1, "ok": true, "data": null}');
	const meta = must(parseChunkMeta(metaFrame), 'the meta frame must parse');

	assert.throws(
		() => reassembleChunks(meta, ['AAAA', ...slices.slice(1)]),
		/checksum mismatch/
	);
});

test('an inline envelope and garbage are classified apart', () => {
	const inline = JSON.stringify({ schema_version: 1, ok: true, data: null });

	assert.deepEqual(transportPlan(inline), { kind: 'inline', payload: inline });
	assert.equal(transportPlan('<gdb truncated this>').kind, 'invalid');
	assert.equal(parseChunkMeta(inline), null);
});
