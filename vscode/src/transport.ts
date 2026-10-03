/**
 * Transport for GDR's machine-readable API.
 *
 * DAP's `evaluate` with a `repl` context never returns command output to the
 * caller -- the adapter streams it to the debug console -- so GDR data arrives
 * through the one channel that does carry a result: a convenience function
 * whose value is a string. `$gdr_json` returns the envelope while it fits one
 * result and a base64 chunk meta frame when it does not; this module owns both
 * command spellings, the reassembly, and the envelope shape.
 *
 * Nothing here imports `vscode`: the transport is pure so it can be unit
 * tested and driven from a plain GDB session (see `test/integration`).
 */

/// <reference types="node" />
// Reason: this subproject keeps its own node_modules, so an editor or `tsc`
// invocation whose analysis root is the repository root would otherwise not
// resolve the Node builtins this file uses.

import { createHash } from 'node:crypto';

/** Envelope schema this client knows how to read. */
export const GDR_SCHEMA_VERSION = 1;

export interface GdrError {
	code: string;
	message: string;
}

export interface GdrEnvelope {
	schema_version: number;
	ok: boolean;
	data: unknown;
	error?: GdrError;
	warnings?: string[];
	rtos?: string | null;
	rtos_version?: string | null;
	gdr_version?: string | null;
	generation?: number;
	/** Wall time of the verb on GDR's side; lets the UI self-throttle. */
	elapsed_ms?: number;
}

export interface ChunkMeta {
	kind: 'chunks';
	schema_version: number;
	total: number;
	chunk_size: number;
	sha256: string;
}

/** A `$gdr_json(verb, ...)` expression for the given verb and arguments. */
export function evaluateExpression(verb: string, ...args: string[]): string {
	return `$gdr_json(${[verb, ...args].map(gdbString).join(', ')})`;
}

/** A `$gdr_json_chunk(index, verb, ...)` expression for one frame. */
export function chunkExpression(index: number, verb: string, ...args: string[]): string {
	return `$gdr_json_chunk(${index}, ${[verb, ...args].map(gdbString).join(', ')})`;
}

/**
 * Quote one argument for GDB's expression parser.
 *
 * GDR reads these with `read_cstring`, so a quote or backslash inside a name
 * would otherwise end the literal early -- object names may legitimately
 * contain both.
 */
function gdbString(value: string): string {
	return `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Parse a GDR envelope, or `null` when the text is not one. */
export function parseEnvelope(text: string): GdrEnvelope | null {
	let parsed: unknown;
	try {
		parsed = JSON.parse(text);
	} catch {
		return null;
	}
	if (!isRecord(parsed)) {
		return null;
	}
	const schema = parsed.schema_version;
	if (typeof schema !== 'number' || typeof parsed.ok !== 'boolean') {
		return null;
	}
	const envelope: GdrEnvelope = {
		schema_version: schema,
		ok: parsed.ok,
		data: parsed.data ?? null
	};
	const error = parsed.error;
	if (isRecord(error) && typeof error.code === 'string' && typeof error.message === 'string') {
		envelope.error = { code: error.code, message: error.message };
	}
	if (Array.isArray(parsed.warnings)) {
		envelope.warnings = parsed.warnings.filter((item): item is string => typeof item === 'string');
	}
	if (typeof parsed.rtos === 'string' || parsed.rtos === null) {
		envelope.rtos = parsed.rtos;
	}
	if (typeof parsed.rtos_version === 'string' || parsed.rtos_version === null) {
		envelope.rtos_version = parsed.rtos_version;
	}
	if (typeof parsed.gdr_version === 'string' || parsed.gdr_version === null) {
		envelope.gdr_version = parsed.gdr_version;
	}
	if (typeof parsed.generation === 'number') {
		envelope.generation = parsed.generation;
	}
	return envelope;
}

/** Parse a chunk meta frame, or `null` when the text is an inline envelope. */
export function parseChunkMeta(text: string): ChunkMeta | null {
	let parsed: unknown;
	try {
		parsed = JSON.parse(text);
	} catch {
		return null;
	}
	if (!isRecord(parsed) || parsed.kind !== 'chunks') {
		return null;
	}
	if (
		typeof parsed.total !== 'number' ||
		typeof parsed.chunk_size !== 'number' ||
		typeof parsed.sha256 !== 'string'
	) {
		return null;
	}
	return {
		kind: 'chunks',
		schema_version: typeof parsed.schema_version === 'number' ? parsed.schema_version : 0,
		total: parsed.total,
		chunk_size: parsed.chunk_size,
		sha256: parsed.sha256
	};
}

/**
 * Reassemble the frames of one transfer and verify the checksum.
 *
 * Throws on a mismatch rather than returning a mixed payload: the frames come
 * from one frozen GDR snapshot, so a checksum failure means the transfer was
 * truncated or spliced, which the caller must not render as data.
 */
export function reassembleChunks(meta: ChunkMeta, slices: string[]): string {
	const payload = Buffer.from(slices.join(''), 'base64').toString('utf8');
	const actual = createHash('sha256').update(payload, 'utf8').digest('hex');
	if (actual !== meta.sha256) {
		throw new Error(
			`GDR chunk checksum mismatch (expected ${meta.sha256}, got ${actual}); ` +
				`the transfer was truncated or spliced`
		);
	}
	return payload;
}

export type TransportPlan =
	| { kind: 'inline'; payload: string }
	| { kind: 'chunked'; meta: ChunkMeta }
	| { kind: 'invalid'; payload: string };

/** Classify one `$gdr_json` result: an inline envelope or a chunked transfer. */
export function transportPlan(text: string): TransportPlan {
	const meta = parseChunkMeta(text);
	if (meta !== null) {
		return { kind: 'chunked', meta };
	}
	if (parseEnvelope(text) !== null) {
		return { kind: 'inline', payload: text };
	}
	return { kind: 'invalid', payload: text };
}

/** A one-line failure message for the UI, including GDR's code. */
export function describeFailure(envelope: GdrEnvelope): string {
	if (!envelope.error) {
		return 'GDR reported a failure without a code';
	}
	return `${envelope.error.code}: ${envelope.error.message}`;
}
