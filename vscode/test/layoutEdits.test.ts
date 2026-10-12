/**
 * Unit tests for the layout gesture rules: where a dragged table lands, the
 * column width bounds, and the layout each gesture produces.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { MAX_COLUMN_WIDTH, MIN_COLUMN_WIDTH, clampWidth, moveKind, withOrder, withWidth } from '../src/webview/layoutEdits.ts';
import type { PanelLayout } from '../src/webview/messages.ts';

const ORDER = ['task', 'timer', 'heap', 'semaphore'];

test('dragging a table before another moves it there and keeps the rest in order', () => {
	assert.deepEqual(moveKind(ORDER, 'semaphore', 'task'), ['semaphore', 'task', 'timer', 'heap']);
	assert.deepEqual(moveKind(ORDER, 'task', 'heap'), ['timer', 'task', 'heap', 'semaphore']);
});

test('dropping a table after the last one, or onto itself, is well defined', () => {
	assert.deepEqual(moveKind(ORDER, 'task', null), ['timer', 'heap', 'semaphore', 'task']);
	assert.deepEqual(moveKind(ORDER, 'heap', 'heap'), ORDER, 'a drop onto itself changes nothing');
});

test('a stray drop of an unknown table or onto an unknown target changes nothing', () => {
	assert.deepEqual(moveKind(ORDER, 'nope', 'task'), ORDER);
	assert.deepEqual(moveKind(ORDER, 'task', 'nope'), ORDER);
});

test('moving never duplicates or drops a table', () => {
	const moved = moveKind(ORDER, 'timer', 'semaphore');
	assert.equal(moved.length, ORDER.length);
	assert.deepEqual([...moved].sort(), [...ORDER].sort());
});

test('a width is clamped to the column bounds and rounded to whole pixels', () => {
	assert.equal(clampWidth(120.6), 121);
	assert.equal(clampWidth(3), MIN_COLUMN_WIDTH);
	assert.equal(clampWidth(99999), MAX_COLUMN_WIDTH);
	assert.equal(clampWidth(Number.NaN), MIN_COLUMN_WIDTH);
});

test('a resize writes the clamped width for that column and leaves the others alone', () => {
	const start: PanelLayout = {
		order: ['task'],
		hiddenTables: [],
		tables: { task: { kind: 'task', widths: { Name: 140 }, hiddenColumns: ['TCB'] } }
	};
	const resized = withWidth(start, 'task', 'PC', 9000);
	assert.deepEqual(resized.tables.task, {
		kind: 'task',
		widths: { Name: 140, PC: MAX_COLUMN_WIDTH },
		hiddenColumns: ['TCB']
	});
});

test('a resize of a table with no saved state creates its entry', () => {
	const resized = withWidth({ order: [], hiddenTables: [], tables: {} }, 'heap', 'Checks', 200);
	assert.deepEqual(resized.tables.heap, { kind: 'heap', widths: { Checks: 200 }, hiddenColumns: [] });
});

test('a reorder changes only the order', () => {
	const start: PanelLayout = { order: [], hiddenTables: ['timer'], tables: {} };
	const next = withOrder(start, ['semaphore', 'task']);
	assert.deepEqual(next.order, ['semaphore', 'task']);
	assert.deepEqual(next.hiddenTables, ['timer']);
});
