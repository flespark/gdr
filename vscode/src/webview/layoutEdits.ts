/**
 * The pure rules behind the panel's layout gestures: where a dragged table
 * lands, how wide a resized column may get, and the layout a gesture makes.
 *
 * The webview client wires the DOM events and calls these; the rules live here
 * so the node test runner can check them without a browser.
 */

import type { PanelLayout } from './messages.ts';

/** A column may not shrink below a readable width nor grow past a screen. */
export const MIN_COLUMN_WIDTH = 40;
export const MAX_COLUMN_WIDTH = 1600;

/** Clamp a dragged width to the column bounds, rounding to whole pixels. */
export function clampWidth(width: number): number {
	if (!Number.isFinite(width)) {
		return MIN_COLUMN_WIDTH;
	}
	return Math.min(MAX_COLUMN_WIDTH, Math.max(MIN_COLUMN_WIDTH, Math.round(width)));
}

/**
 * Move one table kind to the position before another kind (or to the end).
 *
 * `order` is the full list of kinds as drawn. The moved kind is removed and
 * re-inserted; a kind that is not in the list, or a drop onto itself, returns
 * the list unchanged so a stray drop changes nothing.
 */
export function moveKind(order: readonly string[], kind: string, before: string | null): string[] {
	if (!order.includes(kind) || kind === before) {
		return [...order];
	}
	const rest = order.filter((candidate) => candidate !== kind);
	if (before === null) {
		return [...rest, kind];
	}
	const index = rest.indexOf(before);
	if (index === -1) {
		return [...order];
	}
	return [...rest.slice(0, index), kind, ...rest.slice(index)];
}

/** The layout after a table moved; the other tables keep their state. */
export function withOrder(layout: PanelLayout, order: readonly string[]): PanelLayout {
	return { ...layout, order: [...order] };
}

/** The layout after one column of one table was resized to a clamped width. */
export function withWidth(layout: PanelLayout, kind: string, column: string, width: number): PanelLayout {
	const current = layout.tables[kind] ?? { kind, widths: {}, hiddenColumns: [] };
	return {
		...layout,
		tables: {
			...layout.tables,
			[kind]: { ...current, widths: { ...current.widths, [column]: clampWidth(width) } }
		}
	};
}
