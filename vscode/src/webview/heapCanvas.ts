/** Pure heap-grid math: lookups over the grid, plus the layout planner. */
import type { HeapFold, HeapGrid, HeapMapEntry, HeapPalette } from '../snapshot.ts';

// Reason: the interactive resize replays the producer layout locally, so
// these mirror gdr/heapmap.py and gdr/bitmapfont.py one by one. Parity is
// pinned by planHeapGrid tests against producer vectors.
const PLANNER_MAX_CELLS = 16384;
const PLANNER_MIN_PIXEL_WIDTH = 200;
const PLANNER_MAX_PIXEL_WIDTH = 800;
const PLANNER_LINE_STEP = 20;
const PLANNER_LEGEND_GAP = 12;
const PLANNER_LEGEND_CHROME = 36;
const PLANNER_LEGEND_ADVANCE = 12;
const PLANNER_LEGEND_MAX_CHARS = 63;
const PLANNER_LEGEND_MIN_CHARS = 24;
const PLANNER_WRAP_MIN_LINES = 8;
// Reason: exactly gdr/bitmapfont.py `_PATTERNS` keys; anything else escapes
// to `\uXXXX` when measuring wrapped legend lines, like the PNG does.
const PLANNER_PRINTABLE = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 :,.-_/()?\\';

/** One painted unit: a grid row of cells, or one row of a divider band. */
export type HeapUnit = { type: 'row'; row: number } | { type: 'fold'; fold: HeapFold };

/** What the pointer names: a cell's block, a divider's collapsed range, or nothing. */
export type HeapHit =
	| { kind: 'cell'; cell: number }
	| { kind: 'fold'; fold: HeapFold }
	| null;

/** One legend row: the swatch colour and the occupancy text beside it. */
export interface HeapLegendEntry {
	colour: string;
	text: string;
}

/**
 * Paint order in content units: each folded group renders its bands once
 * for all its rows, so one band never splits one row's cells. The record
 * covers `rows` grid rows; heights only stretch bands for the legend floor.
 */
export function unitRows(grid: HeapGrid): HeapUnit[] {
	const byFirst = new Map<number, HeapFold[]>();
	for (const fold of grid.folds ?? []) {
		const list = byFirst.get(fold.row) ?? [];
		list.push(fold);
		byFirst.set(fold.row, list);
	}
	const units: HeapUnit[] = [];
	let row = 0;
	while (row < grid.height) {
		const bands = byFirst.get(row) ?? [];
		if (bands.length > 0) {
			for (const fold of bands) {
				for (let span = 0; span < fold.height; span += 1) { units.push({ type: 'fold', fold }); }
			}
			// Reason: older envelopes carry no row count; one folded row each.
			const covered = Math.max(...bands.map((fold) => fold.rows ?? 1));
			row += covered;
		} else {
			units.push({ type: 'row', row });
			row += 1;
		}
	}
	return units;
}

export function blockAtCell(cell: number, grid: HeapGrid, blocks: readonly HeapMapEntry[]): HeapMapEntry | null {
	if (!Number.isInteger(cell) || cell < 0 || cell >= grid.width * grid.height) { return null; }
	const run = grid.runs.find(([start, stop]) => cell >= start && cell < stop);
	return run === undefined || run[2] < 0 ? null : blocks[run[2]] ?? null;
}

export function locateAtPixel(x: number, y: number, width: number, height: number, grid: HeapGrid): HeapHit {
	if (width <= 0 || height <= 0 || x < 0 || y < 0 || x >= width || y >= height) { return null; }
	const units = unitRows(grid);
	if (units.length === 0) { return null; }
	const unit = units[Math.min(units.length - 1, Math.floor(y / height * units.length))];
	if (unit === undefined || unit.type === 'fold') { return unit === undefined ? null : { kind: 'fold', fold: unit.fold }; }
	const column = Math.floor(x / width * grid.width);
	if (column < 0 || column >= grid.width) { return null; }
	return { kind: 'cell', cell: unit.row * grid.width + column };
}

export function cellAtPixel(x: number, y: number, width: number, height: number, grid: HeapGrid): number | null {
	const hit = locateAtPixel(x, y, width, height, grid);
	return hit !== null && hit.kind === 'cell' ? hit.cell : null;
}

/** Cell-unit corners where a left bar from above meets a top bar from the left. */
export interface HeapCorner {
	column: number;
	row: number;
}

/**
 * Inner corners a wrapping block leaves open: the cell above owns a left
 * divider and the cell to the left owns a top divider, so their shared
 * corner pixel belongs to neither bar. Mirrored by the webview painter.
 */
export function cornerPoints(grid: HeapGrid): HeapCorner[] {
	const owner = new Array<number>(grid.width * grid.height).fill(-1);
	for (const [start, stop, index] of grid.runs) { owner.fill(index, start, stop); }
	const at = (r: number, c: number): number => {
		if (r < 0 || r >= grid.height || c < 0 || c >= grid.width) { return -1; }
		return owner[r * grid.width + c] ?? -1;
	};
	const hasLeft = (r: number, c: number): boolean => {
		if (c <= 0) { return false; }
		const index = at(r, c);
		const left = at(r, c - 1);
		return index >= 0 && left >= 0 && left !== index;
	};
	const hasTop = (r: number, c: number): boolean => {
		if (r <= 0) { return false; }
		const index = at(r, c);
		const up = at(r - 1, c);
		return index >= 0 && up >= 0 && up !== index;
	};
	const corners: HeapCorner[] = [];
	for (let r = 0; r <= grid.height; r += 1) {
		for (let c = 0; c <= grid.width; c += 1) {
			if (hasLeft(r - 1, c) && hasTop(r, c - 1)) { corners.push({ column: c, row: r }); }
		}
	}
	return corners;
}

export function formatAddress(addr: number): string {
	return `0x${addr.toString(16)}`;
}

function addressRange(start: number, size: number): string {
	return `start: ${formatAddress(start)}, end: ${formatAddress(start + size)}`;
}

export function readoutFor(block: HeapMapEntry | null, owners: boolean): string {
	if (block === null) { return 'unknown/unreadable arena span'; }
	const [address, size, used, owner] = block;
	if (!used) { return `owner:(free), size: ${size} B\n${addressRange(address, size)}`; }
	return `owner:${owners && owner !== null ? owner : 'N/A'}, size: ${size} B\n${addressRange(address, size)}`;
}

export function foldReadout(fold: HeapFold): string {
	return `folded: ${fold.bytes} B (${fold.kind})\n${addressRange(fold.start, fold.end - fold.start)}`;
}

/** Bytes selected by `take`, the shared total behind each legend row. */
function sumSizes(blocks: readonly HeapMapEntry[], take: (block: HeapMapEntry) => boolean): number {
	return blocks.reduce((sum, block) => sum + (take(block) ? block[1] : 0), 0);
}

/** The address span selected blocks cover, mirroring the PNG legend rows. */
function spanOf(blocks: readonly HeapMapEntry[], take: (block: HeapMapEntry) => boolean): [number, number] {
	let start = Number.POSITIVE_INFINITY;
	let end = 0;
	for (const block of blocks) {
		if (!take(block)) { continue; }
		start = Math.min(start, block[0]);
		end = Math.max(end, block[0] + block[1]);
	}
	return [start, end];
}

/**
 * The occupancy ranking beside the bitmap, in the PNG legend's own compact
 * order: first-seen owners, then N/A, free, and the unreadable/truncated notes.
 */
export function legendEntries(
	blocks: readonly HeapMapEntry[],
	palette: HeapPalette,
	truncated: boolean,
	hasUnknown: boolean
): HeapLegendEntry[] {
	const owned = new Map<string, HeapMapEntry[]>();
	for (const block of blocks) {
		const [, , used, owner] = block;
		if (used && owner !== null) {
			const list = owned.get(owner) ?? [];
			list.push(block);
			owned.set(owner, list);
		}
	}
	const entries: HeapLegendEntry[] = [];
	for (const [name, list] of owned) {
		const [start, end] = spanOf(list, () => true);
		entries.push({ colour: palette.owners[name] ?? palette.unknown, text: `${name}: ${sumSizes(list, () => true)}B, ${formatAddress(start)}-${formatAddress(end)}` });
	}
	const unowned = blocks.filter((block) => block[2] && block[3] === null);
	if (unowned.length > 0) {
		const [start, end] = spanOf(unowned, () => true);
		entries.push({ colour: palette.unknown, text: `N/A: ${sumSizes(unowned, () => true)}B, ${formatAddress(start)}-${formatAddress(end)}` });
	}
	const free = blocks.filter((block) => !block[2]);
	if (free.length > 0) {
		const [start, end] = spanOf(free, () => true);
		entries.push({ colour: palette.free, text: `(free): ${sumSizes(free, () => true)}B, ${formatAddress(start)}-${formatAddress(end)}` });
	}
	// Reason: notes carry no block colour; a mid grey stays visible on both
	// the dark canvas and a light panel theme, unlike the PNG's dark swatch.
	if (hasUnknown) {
		entries.push({ colour: '#808080', text: 'unknown/unreadable arena span' });
	}
	if (truncated) {
		entries.push({ colour: '#808080', text: 'incomplete block walk' });
	}
	return entries;
}

/** Characters per legend line within what is left of the shared budget. */
function legendCapacity(bitmapWidth: number): number {
	return Math.min(
		PLANNER_LEGEND_MAX_CHARS,
		Math.max(PLANNER_LEGEND_MIN_CHARS, Math.floor((PLANNER_MAX_PIXEL_WIDTH - bitmapWidth - PLANNER_LEGEND_GAP - PLANNER_LEGEND_CHROME) / PLANNER_LEGEND_ADVANCE))
	);
}

/** Printable length: anything outside the bitmap font escapes to `\\uXXXX`. */
function escapedLength(text: string): number {
	let length = 0;
	for (const ch of text) {
		if (PLANNER_PRINTABLE.includes(ch)) { length += 1; continue; }
		const hex = (ch.codePointAt(0) ?? 0).toString(16);
		length += 2 + Math.max(4, hex.length);
	}
	return length;
}

/** Fold text to lines: break after commas first, hard-cut inside fields. */
function wrapLabel(text: string, capacity: number): string[] {
	const lines: string[] = [];
	let current = '';
	for (const field of text.split(', ')) {
		const candidate = current === '' ? field : `${current}, ${field}`;
		if (escapedLength(candidate) <= capacity) { current = candidate; continue; }
		if (current !== '') { lines.push(current); }
		let rest = field;
		while (escapedLength(rest) > capacity) {
			let cut = rest.lastIndexOf(' ', capacity);
			if (cut <= 0) { cut = capacity; }
			lines.push(rest.slice(0, cut));
			rest = rest.slice(cut).replace(/^ +/, '');
		}
		current = rest;
	}
	if (current !== '' || lines.length === 0) { lines.push(current); }
	return lines;
}

/** Legend height in pixels for wrapped texts, mirroring the PNG rule. */
function measureLegendUnits(texts: string[], bitmapWidth: number, usedHeight: number, scale: number): number {
	if (texts.length === 0) { return 0; }
	const capacity = legendCapacity(bitmapWidth);
	const count = texts.reduce((sum, text) => sum + wrapLabel(text, capacity).length, 0);
	const oneColumn = 12 + count * PLANNER_LINE_STEP;
	const lanes = count > PLANNER_WRAP_MIN_LINES && oneColumn > 2 * usedHeight ? 2 : 1;
	return Math.ceil((12 + Math.ceil(count / lanes) * PLANNER_LINE_STEP) / scale);
}

/** One address-ordered span: start, size and block index (or -1 for a gap). */
interface HeapSpan {
	start: number;
	size: number;
	index: number;
}

/** Spans clipped to the arena with unreadable gaps made explicit. */
function heapSpans(blocks: readonly HeapMapEntry[], base: number, end: number): HeapSpan[] {
	const spans: HeapSpan[] = [];
	let cursor = base;
	const ordered = blocks.map((block, index) => ({ block, index })).sort((a, b) => a.block[0] - b.block[0]);
	for (const { block, index } of ordered) {
		const [address, size] = block;
		const low = Math.max(address, base, cursor);
		const high = Math.min(address + size, end);
		if (high <= low) { continue; }
		if (low > cursor) { spans.push({ start: cursor, size: low - cursor, index: -1 }); }
		spans.push({ start: low, size: high - low, index });
		cursor = high;
	}
	if (cursor < end) { spans.push({ start: cursor, size: end - cursor, index: -1 }); }
	return spans;
}

/** Portrait 1:2 rows/columns plus largest-remainder counts (exact fill). */
function distribute(sizes: number[], arena: number, scale: number): { rows: number; columns: number; counts: number[] } {
	const minColumns = Math.ceil(PLANNER_MIN_PIXEL_WIDTH / scale);
	const maxColumns = Math.floor(PLANNER_MAX_PIXEL_WIDTH / scale);
	const desired = Math.max(sizes.length, Math.min(PLANNER_MAX_CELLS, Math.ceil(arena / Math.min(...sizes))));
	let rows = Math.max(1, Math.floor(Math.sqrt(desired * 2)));
	let columns = Math.ceil(desired / rows);
	if (columns > maxColumns) {
		columns = maxColumns;
		rows = Math.ceil(desired / columns);
	}
	if (columns < minColumns) {
		columns = minColumns;
		rows = Math.max(1, Math.ceil(desired / columns));
	}
	const counts = splitCells(sizes, arena, rows * columns, true);
	return { rows, columns, counts };
}

/** Largest-remainder counts filling exactly `total` cells. */
function splitCells(sizes: number[], arena: number, total: number, reserved: boolean): number[] {
	let counts: number[];
	let key: (i: number) => number;
	if (reserved) {
		const remaining = total - sizes.length;
		counts = sizes.map((size) => 1 + Math.floor(remaining * size / arena));
		key = (i: number) => (remaining * sizes[i]!) % arena;
	} else {
		counts = sizes.map((size) => Math.floor(total * size / arena));
		key = (i: number) => (total * sizes[i]!) % arena;
	}
	let extra = total - counts.reduce((sum, count) => sum + count, 0);
	const ranked = sizes.map((_, i) => i).sort((a, b) => key(b) - key(a) || a - b);
	for (const index of ranked) {
		if (extra <= 0) { break; }
		counts[index] = (counts[index] ?? 0) + 1;
		extra -= 1;
	}
	return counts;
}

/** Input for the interactive layout planner: everything but the columns. */
export interface HeapPlanInput {
	blocks: readonly HeapMapEntry[];
	base: number;
	end: number;
	truncated: boolean;
	palette: HeapPalette;
	columns: number;
	scale: number;
}

/**
 * Re-lay the heap grid at a requested column count, replaying the producer
 * algorithm: fold spans wider than one row, snap dividers to row starts,
 * and stretch fold bands up to the legend height. Panel resize only; the
 * envelope grid stays the source of truth for initial paint and the PNG.
 */
export function planHeapGrid(input: HeapPlanInput): HeapGrid {
	const { blocks, base, end, truncated, palette, scale } = input;
	// Reason: only the minimum is clamped; the drag sets whatever width the
	// reader pulls, and the viewport scrolls past the old budget.
	let columns = Math.max(Math.ceil(PLANNER_MIN_PIXEL_WIDTH / scale), Math.round(input.columns));
	const spans = heapSpans(blocks, base, end);
	if (spans.length === 0) { return { width: 0, height: 0, scale, runs: [] }; }
	const arena = spans.reduce((sum, span) => sum + span.size, 0);
	const sizes = spans.map((span) => span.size);
	// Reason: the derived layout is the contract: redoing the envelope width
	// must reproduce the envelope grid exactly, or releasing the drag would jump.
	const derived = distribute(sizes, arena, scale);
	let rows: number;
	let counts: number[];
	if (columns === derived.columns) {
		rows = derived.rows;
		counts = derived.counts;
	} else {
		const desired = Math.max(sizes.length, Math.min(PLANNER_MAX_CELLS, Math.ceil(arena / Math.min(...sizes))));
		rows = Math.max(1, Math.ceil(desired / columns));
		counts = splitCells(sizes, arena, rows * columns, true);
	}
	const runs: [number, number, number][] = [];
	let start = 0;
	spans.forEach((span, position) => {
		runs.push([start, start + (counts[position] ?? 0), span.index]);
		start += counts[position] ?? 0;
	});
	// Per-cell span position and ordinal, for row coverage and fold ranges.
	const cellSpan = new Array<number>(rows * columns).fill(-1);
	const cellOrd = new Array<number>(rows * columns).fill(0);
	let cursor = 0;
	counts.forEach((count, position) => {
		for (let ordinal = 0; ordinal < count; ordinal += 1) {
			cellSpan[cursor] = position;
			cellOrd[cursor] = ordinal;
			cursor += 1;
		}
	});
	const unused = (cell: number): boolean => {
		const position = cellSpan[cell] ?? -1;
		if (position < 0) { return true; }
		const index = spans[position]!.index;
		return index < 0 || blocks[index]?.[2] !== true;
	};
	const folded: number[] = [];
	for (let row = 0; row < rows; row += 1) {
		let covered = true;
		for (let column = 0; column < columns; column += 1) {
			if (!unused(row * columns + column)) { covered = false; break; }
		}
		if (covered) { folded.push(row); }
	}
	if (folded.length === rows) {
		// Reason: an all-unused arena has nothing to emphasize; folding every
		// row would leave an empty grid.
		folded.length = 0;
	}
	const groups: number[][] = [];
	for (const row of folded) {
		const last = groups[groups.length - 1];
		if (last !== undefined && row === last[last.length - 1]! + 1) { last.push(row); }
		else { groups.push([row]); }
	}
	const folds: HeapFold[] = [];
	for (const group of groups) {
		let total = 0;
		let low = -1;
		let high = -1;
		let seenGap = false;
		let seenFree = false;
		for (const row of group) {
			for (let column = 0; column < columns; column += 1) {
				const cell = row * columns + column;
				const position = cellSpan[cell] ?? -1;
				if (position < 0) { continue; }
				const span = spans[position]!;
				const ordinal = cellOrd[cell] ?? 0;
				const count = counts[position] ?? 1;
				const share = span.size / count;
				total += share;
				const first = span.start + ordinal * share;
				low = low < 0 ? first : Math.min(low, first);
				high = Math.max(high, first + share);
				if (span.index < 0) { seenGap = true; } else { seenFree = true; }
			}
		}
		let kind = 'free';
		if (seenGap && seenFree) { kind = 'mixed'; } else if (seenGap) { kind = 'unknown'; }
		folds.push({ row: group[0]!, rows: group.length, height: 1, bytes: Math.round(total), kind, start: Math.round(low), end: Math.round(high) });
	}
	if (folds.length > 0) {
		const hasUnknown = runs.some((run) => run[2] < 0) || folds.some((fold) => fold.kind !== 'free');
		const texts = legendEntries(blocks, palette, truncated, hasUnknown).map((entry) => entry.text);
		// Reason: each folded group paints as one band, so the base is the
		// kept rows plus one unit per group.
		const legendUnits = measureLegendUnits(texts, columns * scale, rows * scale, scale);
		const base = rows - folded.length + folds.length;
		const required = Math.max(base, legendUnits);
		let extra = required - base;
		let i = 0;
		while (extra > 0) {
			folds[i % folds.length]!.height += 1;
			extra -= 1;
			i += 1;
		}
	}
	return { width: columns, height: rows, scale, runs, folds };
}
