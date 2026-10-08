/**
 * The snapshot envelope, parsed into the shapes the panel renders.
 *
 * Everything in here is pure: the snapshot is GDR's single source of truth,
 * and the panel never decodes kernel state of its own -- it only narrows the
 * JSON the adapter already serialized (columns, machine values, verdicts).
 * The only arithmetic done here is presentation-level (ratios between two
 * numeric cells), never interpretation of a field.
 *
 * A snapshot costs target reads (the heap walk most of all), so the cost
 * switches live next to the types: `--no-heap` skips the one full-memory
 * traversal among the panel verbs, and the freshness guard drops responses
 * that raced a newer stop.
 */

import type { GdrEnvelope } from './transport.ts';

export interface ColumnSpec {
	key: string;
	kind: string;
	align: 'left' | 'right';
	elastic: boolean;
	doc: string;
}

/** Where a cell's code lives, when GDR could resolve it to a source line. */
export interface SourceRef {
	file: string;
	line: number;
}

export interface ObjectTableData {
	headers: string[];
	rows: string[][];
	messages: string[];
	elastic: string[];
	kind?: string;
	columns?: ColumnSpec[];
	/** Machine values beside the rendered text; mirrors `rows` exactly. */
	value_rows?: unknown[][];
	/** Source locations beside the rendered text; `null` where none is known. */
	source_rows?: (SourceRef | null)[][];
	/** Stable row identities (the `id_key` column's raw value). */
	ids?: string[];
}

export interface KindCapability {
	enumerable: boolean;
	detail: string;
	columns: string[];
	note: string;
}

export interface CapabilitiesData {
	kinds: Record<string, KindCapability>;
	extra: string[];
}

export interface CheckFinding {
	name: string;
	verdict: string;
	detail: string;
	scope: string;
}

export interface ChecksData {
	findings: CheckFinding[];
	counts: Record<string, number>;
}

export interface HeapData {
	pairs: string[][];
	messages: string[];
	table: ObjectTableData | null;
	/** What the table lists, e.g. free blocks or used blocks by thread. */
	tableTitle: string;
}

export interface SystemData {
	pairs: string[][];
}

/** One snapshot, with the envelope metadata that governs its display. */
export interface PanelSnapshot {
	rtos: string | null;
	rtosVersion: string | null;
	gdrVersion: string | null;
	generation: number | null;
	elapsedMs: number | null;
	collectedAt: number;
	warnings: string[];
	noHeap: boolean;
	data: SnapshotData;
}

export interface SnapshotData {
	tasks: ObjectTableData | null;
	objects: ObjectTableData | null;
	system: SystemData | null;
	heap: HeapData | null;
	capabilities: CapabilitiesData | null;
	checks: ChecksData | null;
}

export interface CostOptions {
	/** Skip the heap section (the only full-memory walk among the verbs). */
	noHeap: boolean;
}

/** The snapshot arguments a caller issues for its cost options. */
export function costArgs(options: CostOptions): string[] {
	return options.noHeap ? ['--no-heap'] : [];
}

/**
 * How a snapshot crosses from GDB to the panel.
 *
 * ``evaluate`` is the default: the envelope rides DAP ``evaluate`` results
 * (inline, or base64 frames when large). ``file`` asks GDB to write the
 * envelope to a file and reads it back, which is cheaper for very large
 * snapshots but needs the extension to read the filesystem GDB writes to.
 */
export type SnapshotTransport = 'evaluate' | 'file';

/** Decode the setting at the boundary; anything unrecognized is the default. */
export function parseSnapshotTransport(value: unknown): SnapshotTransport {
	return value === 'file' ? 'file' : 'evaluate';
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asStringList(value: unknown): string[] {
	return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function asStrings(value: unknown): string[][] {
	if (!Array.isArray(value)) {
		return [];
	}
	const pairs: string[][] = [];
	for (const item of value) {
		if (!Array.isArray(item)) {
			continue;
		}
		const strings = item.filter((cell): cell is string => typeof cell === 'string');
		pairs.push(strings);
	}
	return pairs;
}

function parseColumnSpecs(value: unknown): ColumnSpec[] | undefined {
	if (!Array.isArray(value)) {
		return undefined;
	}
	const specs: ColumnSpec[] = [];
	for (const item of value) {
		if (!isRecord(item) || typeof item.key !== 'string') {
			continue;
		}
		specs.push({
			key: item.key,
			kind: typeof item.kind === 'string' ? item.kind : 'text',
			align: item.align === 'right' ? 'right' : 'left',
			elastic: item.elastic === true,
			doc: typeof item.doc === 'string' ? item.doc : ''
		});
	}
	return specs.length > 0 ? specs : undefined;
}

function parseSourceRef(value: unknown): SourceRef | null {
	if (!isRecord(value) || typeof value.file !== 'string') {
		return null;
	}
	if (typeof value.line !== 'number' || !Number.isInteger(value.line) || value.line < 1) {
		return null;
	}
	return { file: value.file, line: value.line };
}

function parseTable(value: unknown): ObjectTableData | null {
	if (!isRecord(value) || !Array.isArray(value.headers) || !Array.isArray(value.rows)) {
		return null;
	}
	const table: ObjectTableData = {
		headers: asStringList(value.headers),
		rows: asStrings(value.rows),
		messages: asStringList(value.messages),
		elastic: asStringList(value.elastic)
	};
	if (typeof value.kind === 'string') {
		table.kind = value.kind;
	}
	const columns = parseColumnSpecs(value.columns);
	if (columns !== undefined) {
		table.columns = columns;
	}
	if (Array.isArray(value.value_rows)) {
		table.value_rows = value.value_rows.map((row) => (Array.isArray(row) ? row : []));
	}
	if (Array.isArray(value.source_rows)) {
		table.source_rows = value.source_rows.map((row) =>
			Array.isArray(row) ? row.map(parseSourceRef) : []
		);
	}
	if (Array.isArray(value.ids)) {
		table.ids = asStringList(value.ids);
	}
	return table.headers.length > 0 ? table : null;
}

function parseCapabilities(value: unknown): CapabilitiesData | null {
	if (!isRecord(value) || !isRecord(value.kinds)) {
		return null;
	}
	const kinds: Record<string, KindCapability> = {};
	for (const [kind, raw] of Object.entries(value.kinds)) {
		if (!isRecord(raw)) {
			continue;
		}
		kinds[kind] = {
			enumerable: raw.enumerable === true,
			detail: typeof raw.detail === 'string' ? raw.detail : '',
			columns: asStringList(raw.columns),
			note: typeof raw.note === 'string' ? raw.note : ''
		};
	}
	return { kinds, extra: asStringList(value.extra) };
}

function parseChecks(value: unknown): ChecksData | null {
	if (!isRecord(value) || !Array.isArray(value.findings)) {
		return null;
	}
	const findings: CheckFinding[] = [];
	for (const raw of value.findings) {
		if (!isRecord(raw) || typeof raw.name !== 'string' || typeof raw.verdict !== 'string') {
			continue;
		}
		findings.push({
			name: raw.name,
			verdict: raw.verdict,
			detail: typeof raw.detail === 'string' ? raw.detail : '',
			scope: typeof raw.scope === 'string' ? raw.scope : ''
		});
	}
	const counts: Record<string, number> = {};
	if (isRecord(value.counts)) {
		for (const [verdict, count] of Object.entries(value.counts)) {
			if (typeof count === 'number') {
				counts[verdict] = count;
			}
		}
	}
	return { findings, counts };
}

/**
 * Read a snapshot out of an envelope's `data`, or `null`.
 *
 * A message that is a valid envelope but not a snapshot (a `gdr api` error the
 * transport still delivered) returns `null` so the caller can report the
 * failure instead of rendering an empty panel.
 */
export function parseSnapshot(data: unknown): SnapshotData | null {
	if (!isRecord(data)) {
		return null;
	}
	const snapshot = {
		tasks: parseTable(data.tasks),
		objects: parseTable(data.objects),
		system: isRecord(data.system) ? { pairs: asStrings(data.system.pairs) } : null,
		heap: parseHeap(data.heap),
		capabilities: parseCapabilities(data.capabilities),
		checks: parseChecks(data.checks)
	};
	// Reason: the snapshot verb always serializes every section (some maybe
	// empty), so an envelope whose data carries none of them is not a
	// snapshot at all -- a probe result or an error payload, which the caller
	// must not render as an empty panel.
	if (
		snapshot.tasks === null &&
		snapshot.objects === null &&
		snapshot.system === null &&
		snapshot.heap === null &&
		snapshot.capabilities === null &&
		snapshot.checks === null
	) {
		return null;
	}
	return snapshot;
}

function parseHeap(value: unknown): HeapData | null {
	if (!isRecord(value)) {
		return null;
	}
	return {
		pairs: asStrings(value.pairs),
		messages: asStringList(value.messages),
		table: parseTable(value.table),
		tableTitle: typeof value.table_title === 'string' ? value.table_title : 'Heap blocks'
	};
}

/**
 * The panel-side freshness guard.
 *
 * GDR bumps the generation on stop, objfile load and reset; an envelope whose
 * generation trails the one already shown answered an older stop and must be
 * dropped even if it arrived later. A `null` generation (a runtime older than
 * the guard) is never considered stale on its own.
 */
export function isStale(lastGeneration: number | null, next: GdrEnvelope): boolean {
	if (lastGeneration === null || typeof next.generation !== 'number') {
		return false;
	}
	return next.generation <= lastGeneration;
}
