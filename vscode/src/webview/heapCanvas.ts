/** Pure lookups over the Python-generated heap grid (painting and hover agree). */
import type { HeapGrid, HeapMapEntry } from '../snapshot.ts';

export function blockAtCell(cell: number, grid: HeapGrid, blocks: readonly HeapMapEntry[]): HeapMapEntry | null {
	if (!Number.isInteger(cell) || cell < 0 || cell >= grid.width * grid.height) { return null; }
	const run = grid.runs.find(([start, stop]) => cell >= start && cell < stop);
	return run === undefined || run[2] < 0 ? null : blocks[run[2]] ?? null;
}

export function cellAtPixel(x: number, y: number, width: number, height: number, grid: HeapGrid): number | null {
	if (width <= 0 || height <= 0 || x < 0 || y < 0 || x >= width || y >= height) { return null; }
	const column = Math.floor(x / width * grid.width);
	const row = Math.floor(y / height * grid.height);
	return row * grid.width + column;
}

export function readoutFor(block: HeapMapEntry | null, owners: boolean): string {
	if (block === null) { return 'unknown/unreadable arena span'; }
	const [, size, used, owner] = block;
	if (!used) { return `owner:(free), size: ${size} B`; }
	return `owner:${owners && owner !== null ? owner : 'N/A'}, size: ${size} B`;
}
