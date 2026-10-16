/**
 * The snapshot envelope, parsed into the shapes the panel renders.
 *
 * Everything in here is pure: the snapshot is GDR's single source of truth,
 * and the panel never decodes kernel state of its own -- it only narrows the
 * JSON the adapter already serialized (columns, machine values, verdicts).
 * The only arithmetic done here is presentation-level (ratios between two
 * numeric cells), never interpretation of a field.
 *
 * A snapshot costs target reads (the heap walk and the per-kind detail tables
 * most of all), so the cost switches live next to the types: `--no-heap` skips
 * the full-memory heap walk, `--no-details` skips the per-object detail walks,
 * and the freshness guard drops responses that raced a newer stop.
 */

import type { GdrEnvelope } from './transport.ts';

/** How a column is grouped in the panel's child checkboxes. */
export type ColumnGroup = 'basic' | 'diag';

export interface ColumnSpec {
	key: string;
	kind: string;
	align: 'left' | 'right';
	elastic: boolean;
	doc: string;
	group: ColumnGroup;
}

/**
 * Where a cell's code lives, when GDR could resolve it to a source line.
 *
 * `file` is GDB's resolved path (possibly a build directory absent here);
 * `name` is the path as the debug information recorded it. The source resolver
 * tries `file` first and falls back to matching `name` inside the workspace.
 */
export interface SourceRef {
	file: string;
	name: string;
	line: number;
}

export interface ObjectTableData {
	buildHints?: string[];
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
	buildHints?: string[];
	kinds: Record<string, KindCapability>;
	extra: string[];
}

/** One consistency verdict row inside a heap or detail section. */
export interface VerdictRow {
	key: string;
	value: string;
	severity: string;
}

export interface VerdictSection {
	title: string;
	rows: VerdictRow[];
}

export interface HeapData {
	pairs: string[][];
	messages: string[];
	sections: VerdictSection[];
}

export interface SystemData {
	pairs: string[][];
}

/** One heap block as the heap-dump envelope lists it: `[address, size, used, owner]`. */
export type HeapMapEntry = [number, number, boolean, string | null];

/** The colours the bitmap uses, so the panel's drawing matches the PNG. */
export interface HeapPalette {
	owners: Record<string, string>;
	free: string;
	unknown: string;
}

/** One collapsed unused range: hatched bands drawn before grid `row`. */
export interface HeapFold {
	row: number;
	rows: number;
	height: number;
	bytes: number;
	kind: string;
	start: number;
	end: number;
}

/** The heap block layout behind `<rtos> heap dump`, drawn on demand. */
export interface HeapGrid {
	width: number;
	height: number;
	scale: number;
	runs: [number, number, number][];
	folds?: HeapFold[];
}

export interface HeapMapData {
	grid?: HeapGrid;
	algorithm: string;
	base: number;
	end: number;
	owners: boolean;
	truncated: boolean;
	messages: string[];
	blocks: HeapMapEntry[];
	palette: HeapPalette;
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
	noDetails: boolean;
	data: SnapshotData;
}

export interface SnapshotData {
	/** Adapter-produced actionable advice; never general messages or errors. */
	buildHints?: string[];
	tasks: ObjectTableData | null;
	/** One merged detail table per enumerable kind (tasks excluded); `null` when skipped. */
	tables: Record<string, ObjectTableData> | null;
	system: SystemData | null;
	heap: HeapData | null;
	capabilities: CapabilitiesData | null;
}

export interface CostOptions {
	/** Skip the heap section (the only full-memory walk among the verbs). */
	noHeap: boolean;
	/** Skip the per-kind detail tables (each walks its objects' entries). */
	noDetails: boolean;
}

/** The snapshot arguments a caller issues for its cost options. */
export function costArgs(options: CostOptions): string[] {
	const args: string[] = [];
	if (options.noHeap) {
		args.push('--no-heap');
	}
	if (options.noDetails) {
		args.push('--no-details');
	}
	return args;
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
			doc: typeof item.doc === 'string' ? item.doc : '',
			group: item.group === 'diag' ? 'diag' : 'basic'
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
	// Reason: an older runtime omits `name`; the file is then the only path there is.
	const name = typeof value.name === 'string' ? value.name : value.file;
	return { file: value.file, name, line: value.line };
}

function parseTable(value: unknown): ObjectTableData | null {
	if (!isRecord(value) || !Array.isArray(value.headers) || !Array.isArray(value.rows)) {
		return null;
	}
	const table: ObjectTableData = {
		buildHints: asStringList(value.build_hints),
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
		table.ids = value.ids.map((id) => typeof id === 'string' || typeof id === 'number' ? String(id) : '');
	}
	return table.headers.length > 0 ? table : null;
}

function parseTables(value: unknown): Record<string, ObjectTableData> | null {
	if (!isRecord(value)) {
		return null;
	}
	const tables: Record<string, ObjectTableData> = {};
	for (const [kind, raw] of Object.entries(value)) {
		const table = parseTable(raw);
		if (table !== null) {
			tables[kind] = table;
		}
	}
	return tables;
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
	return { kinds, extra: asStringList(value.extra), buildHints: asStringList(value.build_hints) };
}

function parseSections(value: unknown): VerdictSection[] {
	if (!Array.isArray(value)) {
		return [];
	}
	const sections: VerdictSection[] = [];
	for (const raw of value) {
		if (!isRecord(raw) || typeof raw.title !== 'string' || !Array.isArray(raw.rows)) {
			continue;
		}
		const rows: VerdictRow[] = [];
		for (const row of raw.rows) {
			if (!isRecord(row) || typeof row.key !== 'string') {
				continue;
			}
			rows.push({
				key: row.key,
				value: typeof row.value === 'string' ? row.value : '',
				severity: typeof row.severity === 'string' ? row.severity : 'info'
			});
		}
		sections.push({ title: raw.title, rows });
	}
	return sections;
}

function parseHeap(value: unknown): HeapData | null {
	if (!isRecord(value)) {
		return null;
	}
	return {
		pairs: asStrings(value.pairs),
		messages: asStringList(value.messages),
		sections: parseSections(value.sections)
	};
}

function parsePalette(value: unknown): HeapPalette | null {
	if (!isRecord(value) || typeof value.free !== 'string' || typeof value.unknown !== 'string') {
		return null;
	}
	const owners: Record<string, string> = {};
	if (isRecord(value.owners)) {
		for (const [owner, colour] of Object.entries(value.owners)) {
			if (typeof colour === 'string') {
				owners[owner] = colour;
			}
		}
	}
	return { owners, free: value.free, unknown: value.unknown };
}

/** Narrow one heap-dump block row; a malformed row is dropped, never guessed. */
function parseHeapBlock(value: unknown): HeapMapEntry | null {
	if (!Array.isArray(value) || value.length !== 4) {
		return null;
	}
	const [address, size, used, owner] = value;
	if (typeof address !== 'number' || typeof size !== 'number' || typeof used !== 'boolean') {
		return null;
	}
	return [address, size, used, typeof owner === 'string' ? owner : null];
}

/**
 * Read the heap-dump envelope (`<rtos> heap dump` as JSON), or `null`.
 *
 * The blocks are the only data the hover readout needs; the palette is what
 * the canvas paints with, so both come from the same envelope.
 */
/** Narrow one divider record; a malformed record rejects the grid, never guessed. */
function parseHeapFold(value: unknown, height: number): HeapFold | null {
	if (!isRecord(value)) { return null; }
	const { row, rows, height: foldHeight, bytes, kind, start, end } = value;
	if (!Number.isInteger(row) || (row as number) < 0 || (row as number) > height) { return null; }
	// Reason: older envelopes carry no row count; one folded row each.
	const covered = rows === undefined ? 1 : rows;
	if (!Number.isInteger(covered) || (covered as number) < 1 ||
		(row as number) + (covered as number) > height) { return null; }
	if (!Number.isInteger(foldHeight) || (foldHeight as number) < 1) { return null; }
	if (!Number.isInteger(bytes) || (bytes as number) < 0) { return null; }
	if (typeof kind !== 'string' || !Number.isInteger(start) || !Number.isInteger(end) ||
		(start as number) < 0 || (end as number) < (start as number)) { return null; }
	return { row: row as number, rows: covered as number, height: foldHeight as number, bytes: bytes as number, kind, start: start as number, end: end as number };
}

/** Narrow the divider list; bands must arrive sorted with strictly rising rows. */
function parseHeapFolds(value: unknown, height: number): HeapFold[] | null {
	if (!Array.isArray(value)) { return null; }
	const folds: HeapFold[] = [];
	let lastEnd = -1;
	for (const raw of value) {
		const fold = parseHeapFold(raw, height);
		if (fold === null || fold.row <= lastEnd) { return null; }
		lastEnd = fold.row + fold.rows;
		folds.push(fold);
	}
	return folds;
}

function parseHeapGrid(value: unknown): HeapGrid | null {
	if (!isRecord(value)) { return null; }
	const { width, height, scale, runs } = value;
	if (typeof width !== 'number' || typeof height !== 'number' || typeof scale !== 'number' ||
		!Number.isInteger(width) || !Number.isInteger(height) || !Number.isInteger(scale) ||
		width < 0 || height < 0 || width * height > 65536 || scale < 1 || scale > 64 || !Array.isArray(runs)) { return null; }
	let cursor = 0;
	for (const run of runs) {
		if (!Array.isArray(run) || run.length !== 3 || !run.every(Number.isInteger) || run[0] !== cursor || run[1] <= cursor || run[1] > width * height || run[2] < -1) { return null; }
		cursor = run[1];
	}
	if (cursor !== width * height) { return null; }
	const grid: HeapGrid = { width, height, scale, runs: runs as [number, number, number][] };
	// Reason: divider bands arrive sorted with strictly rising rows; an
	// out-of-order band would paint over the wrong cells, so it rejects the
	// grid. Retired gutter ticks stay ignored so an older envelope still draws.
	if (value.folds !== undefined) {
		const folds = parseHeapFolds(value.folds, height);
		if (folds === null) { return null; }
		grid.folds = folds;
	}
	return grid;
}

export function parseHeapMap(data: unknown): HeapMapData | null {
	if (!isRecord(data) || typeof data.algorithm !== 'string') {
		return null;
	}
	if (typeof data.base !== 'number' || typeof data.end !== 'number') {
		return null;
	}
	const palette = parsePalette(data.palette);
	if (palette === null || !Array.isArray(data.blocks)) {
		return null;
	}
	const blocks: HeapMapEntry[] = [];
	for (const raw of data.blocks) {
		const block = parseHeapBlock(raw);
		if (block !== null) {
			blocks.push(block);
		}
	}
	const grid = parseHeapGrid(data.grid);
	// Reason: dropping a malformed block changes indexes; never use a run map
	// against a different list than the producer used.
	if (data.grid !== undefined && (grid === null || blocks.length !== data.blocks.length || grid.runs.some((run) => run[2] >= blocks.length))) { return null; }
	return {
		...(grid !== null ? { grid } : {}),
		algorithm: data.algorithm,
		base: data.base,
		end: data.end,
		owners: data.owners === true,
		truncated: data.truncated === true,
		messages: asStringList(data.messages),
		blocks,
		palette
	};
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
	const snapshot: SnapshotData = {
		tasks: parseTable(data.tasks),
		tables: parseTables(data.tables),
		system: isRecord(data.system) ? { pairs: asStrings(data.system.pairs) } : null,
		heap: parseHeap(data.heap),
		capabilities: parseCapabilities(data.capabilities),
		buildHints: asStringList(data.build_hints)
	};
	// Reason: the snapshot verb always serializes every section (some maybe
	// empty), so an envelope whose data carries none of them is not a
	// snapshot at all -- a probe result or an error payload, which the caller
	// must not render as an empty panel.
	if (
		snapshot.tasks === null &&
		snapshot.tables === null &&
		snapshot.system === null &&
		snapshot.heap === null &&
		snapshot.capabilities === null
	) {
		return null;
	}
	return snapshot;
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
