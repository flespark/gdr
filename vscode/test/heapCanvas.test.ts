import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { HeapGrid, HeapMapEntry } from '../src/snapshot.ts';
import { blockAtCell, cellAtPixel, readoutFor } from '../src/webview/heapCanvas.ts';

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
	assert.equal(cellAtPixel(-1, 0, 24, 12, grid), null);
	assert.equal(cellAtPixel(0, 0, 0, 12, grid), null);
});

test('readout reports original block sizes, free and unknown owners', () => {
	assert.equal(readoutFor(blocks[0], true), 'owner:main, size: 1 B');
	assert.equal(readoutFor(blocks[0], false), 'owner:N/A, size: 1 B');
	assert.equal(readoutFor(blocks[1], true), 'owner:(free), size: 100 B');
	assert.equal(readoutFor([101, 8, true, null], true), 'owner:N/A, size: 8 B');
	assert.equal(readoutFor(null, true), 'unknown/unreadable arena span');
});
