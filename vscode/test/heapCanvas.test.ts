import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { HeapGrid, HeapMapEntry } from '../src/snapshot.ts';
import { blockAtCell, cellAtPixel, cornerPoints, foldReadout, formatAddress, legendEntries, locateAtPixel, planHeapGrid, readoutFor, unitRows } from '../src/webview/heapCanvas.ts';

const grid: HeapGrid = { width: 4, height: 2, scale: 6, runs: [[0, 1, 0], [1, 7, 1], [7, 8, -1]] };
const blocks: HeapMapEntry[] = [[0, 1, true, 'main'], [1, 100, false, null]];

test('hover follows serialized runs, including a tiny block and an unknown gap', () => {
	assert.deepEqual(blockAtCell(0, grid, blocks), blocks[0]);
	assert.deepEqual(blockAtCell(6, grid, blocks), blocks[1]);
	assert.equal(blockAtCell(7, grid, blocks), null);
	assert.equal(blockAtCell(8, grid, blocks), null);
	assert.equal(blockAtCell(-1, grid, blocks), null);
});

test('pointer coordinates use the adaptive dimensions and reject outside edges', () => {
	assert.equal(cellAtPixel(0, 0, 24, 12, grid), 0);
	assert.equal(cellAtPixel(23, 11, 24, 12, grid), 7);
	assert.equal(cellAtPixel(24, 0, 24, 12, grid), null);
	assert.equal(cellAtPixel(24, 12, 24, 12, grid), null);
	assert.equal(cellAtPixel(-1, 0, 24, 12, grid), null);
	assert.equal(cellAtPixel(0, 0, 0, 12, grid), null);
});

test('readout reports original block sizes, free and unknown owners', () => {
	assert.equal(readoutFor(blocks[0], true), 'owner:main, size: 1 B\nstart: 0x0, end: 0x1');
	assert.equal(readoutFor(blocks[0], false), 'owner:N/A, size: 1 B\nstart: 0x0, end: 0x1');
	assert.equal(readoutFor(blocks[1], true), 'owner:(free), size: 100 B\nstart: 0x1, end: 0x65');
	assert.equal(readoutFor([101, 8, true, null], true), 'owner:N/A, size: 8 B\nstart: 0x65, end: 0x6d');
	assert.equal(readoutFor(null, true), 'unknown/unreadable arena span');
});

test('addresses print as full-precision hex', () => {
	assert.equal(formatAddress(0), '0x0');
	assert.equal(formatAddress(0x20000000), '0x20000000');
});

test('divider bands replace their grid row instead of preceding it', () => {
	const folded: HeapGrid = {
		width: 2, height: 2, scale: 6, runs: [[0, 4, 0]],
		folds: [{ row: 1, rows: 1, height: 3, bytes: 500, kind: 'free', start: 64, end: 564 }]
	};
	assert.deepEqual(unitRows(folded).map((unit) => unit.type), ['row', 'fold', 'fold', 'fold']);
	assert.deepEqual(locateAtPixel(0, 0, 12, 24, folded), { kind: 'cell', cell: 0 });
	assert.deepEqual(locateAtPixel(11, 8, 12, 24, folded), { kind: 'fold', fold: folded.folds![0] });
	assert.deepEqual(locateAtPixel(0, 20, 12, 24, folded), { kind: 'fold', fold: folded.folds![0] });
	assert.equal(cellAtPixel(0, 8, 12, 24, folded), null);
});

test('a wrapping block closes its inner corner where bars meet', () => {
	const wrapped: HeapGrid = { width: 20, height: 2, scale: 6, runs: [[0, 2, 0], [2, 38, 1], [38, 40, 2]] };
	assert.deepEqual(cornerPoints(wrapped), [{ column: 2, row: 1 }]);
	const single: HeapGrid = { width: 4, height: 2, scale: 6, runs: [[0, 1, 0], [1, 7, 1], [7, 8, -1]] };
	assert.deepEqual(cornerPoints(single), [{ column: 1, row: 1 }]);
	const solid: HeapGrid = { width: 2, height: 1, scale: 6, runs: [[0, 2, 0]] };
	assert.deepEqual(cornerPoints(solid), []);
});

test('the planner reproduces the producer grid at the envelope width', () => {
	const palette = { owners: {}, free: '#c8c8c8', unknown: '#4f8fd6' };
	const tiny = planHeapGrid({ blocks: [[0, 1, true, null]], base: 0, end: 65536000, truncated: false, palette, columns: 80, scale: 10 });
	assert.deepEqual(
		{ width: tiny.width, height: tiny.height, scale: tiny.scale, runs: tiny.runs },
		{ width: 80, height: 205, scale: 10, runs: [[0, 1, 0], [1, 16400, -1]] }
	);
	assert.equal(tiny.folds!.length, 1, 'the whole folded tail is one band');
	assert.deepEqual(tiny.folds![0], { row: 1, rows: 204, height: 7, bytes: 65220288, kind: 'unknown', start: 315712, end: 65536000 });
	assert.deepEqual(
		planHeapGrid({
			blocks: [[0, 16, true, 'main'], [16, 16, false, null]], base: 0, end: 32, truncated: false,
			palette: { owners: { main: '#e15e3a' }, free: '#c8c8c8', unknown: '#4f8fd6' }, columns: 20, scale: 10
		}),
		{ width: 20, height: 1, scale: 10, runs: [[0, 10, 0], [10, 20, 1]], folds: [] }
	);
});

test('a forced width retiles runs and refolds rows without losing bytes', () => {
	const palette = { owners: {}, free: '#c8c8c8', unknown: '#4f8fd6' };
	const input = { blocks: [[0, 1, true, null]] as [number, number, boolean, string | null][], base: 0, end: 65536000, truncated: false, palette, columns: 20, scale: 10 };
	const narrow = planHeapGrid(input);
	assert.equal(narrow.width, 20);
	assert.equal((narrow.folds ?? []).length, 1, 'one contiguous tail stays one band');
	assert.ok(narrow.folds![0]!.rows > 200, 'narrow widths fold many rows into it');
	const wide = planHeapGrid({ ...input, columns: 80 });
	assert.equal(wide.width, 80);
	assert.deepEqual(wide.runs, [[0, 1, 0], [1, 16400, -1]]);
	const foldedBytes = (grid: typeof wide): number =>
		(grid.folds ?? []).reduce((sum, fold) => sum + fold.bytes, 0);
	assert.ok(narrow.folds![0]!.rows > wide.folds![0]!.rows, 'narrowing folds more rows into the band');
	assert.ok(foldedBytes(narrow) > foldedBytes(wide), 'and the band carries more bytes');
	assert.ok(foldedBytes(wide) > 0 && foldedBytes(wide) < 65535999);
	const grown = planHeapGrid({ ...input, columns: 200 });
	assert.equal(grown.width, 200, 'drag widths no longer clamp to a maximum');
	assert.equal(grown.runs[grown.runs.length - 1]![1], 200 * grown.height, 'wide grids still tile exactly');
});

test('fold readout names the collapsed bytes, kind and address span', () => {
	assert.equal(
		foldReadout({ row: 1, rows: 1, height: 2, bytes: 500, kind: 'free', start: 64, end: 564 }),
		'folded: 500 B (free)\nstart: 0x40, end: 0x234'
	);
});

test('legend ranks compact first-seen owners with spans, then N/A, free and notes', () => {
	const entries = legendEntries(
		[[0, 100, true, 'worker'], [100, 300, true, 'main'], [400, 50, true, null], [450, 16, false, null]],
		{ owners: { worker: '#111111', main: '#222222' }, free: '#c8c8c8', unknown: '#4f8fd6' },
		true,
		true
	);
	assert.deepEqual(entries.map((entry) => entry.text), [
		'worker: 100B, 0x0-0x64',
		'main: 300B, 0x64-0x190',
		'N/A: 50B, 0x190-0x1c2',
		'(free): 16B, 0x1c2-0x1d2',
		'unknown/unreadable arena span',
		'incomplete block walk'
	]);
	assert.equal(entries[0]!.colour, '#111111');
});

test('legend notes the unreadable span only when a gap says so', () => {
	const palette = { owners: {}, free: '#c8c8c8', unknown: '#4f8fd6' };
	const clean = legendEntries([[0, 64, true, null]], palette, false, false);
	assert.ok(!clean.some((entry) => entry.text.includes('unknown')));
	assert.deepEqual(clean.map((entry) => entry.text), ['N/A: 64B, 0x0-0x40']);
	const gapped = legendEntries([[0, 64, true, null]], palette, false, true);
	assert.ok(gapped.some((entry) => entry.text === 'unknown/unreadable arena span'));
});
