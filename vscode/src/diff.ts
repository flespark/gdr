/**
 * Which values changed since the last applied snapshot.
 *
 * The baseline is the snapshot the panel last showed, so a value is highlighted
 * when a previous value for the same cell exists and differs. The first stop has
 * no baseline and highlights nothing; a row that did not exist before has no
 * previous cell to compare and is not highlighted either.
 *
 * Cells are keyed the same way the renderer keys them (`changeKey`): table kind,
 * row identity, column. System and heap summaries have no row identity, so
 * their pairs are keyed by the pair's label.
 */

import type { ObjectTableData, PanelSnapshot } from './snapshot.ts';
import { changeKey } from './webview/render.ts';

/** The cell values of one snapshot, keyed the way the renderer keys its cells. */
export function cellValues(snapshot: PanelSnapshot): Map<string, string> {
	const values = new Map<string, string>();
	const addTable = (kind: string, table: ObjectTableData | null): void => {
		if (table === null) {
			return;
		}
		table.rows.forEach((row, rowIndex) => {
			const id = String(table.ids?.[rowIndex] ?? '');
			table.headers.forEach((column, columnIndex) => {
				values.set(changeKey(kind, id, column), JSON.stringify([row[columnIndex] ?? '', table.value_rows?.[rowIndex]?.[columnIndex] ?? null]));
			});
		});
	};
	addTable('task', snapshot.data.tasks);
	for (const [kind, table] of Object.entries(snapshot.data.tables ?? {})) {
		addTable(kind, table);
	}
	for (const [label, value] of snapshot.data.system?.pairs ?? []) {
		values.set(changeKey('system', '', label), value);
	}
	for (const [label, value] of snapshot.data.heap?.pairs ?? []) {
		values.set(changeKey('heap', '', label), value);
	}
	for (const section of snapshot.data.heap?.sections ?? []) {
		for (const row of section.rows) { values.set(changeKey('heap', '', row.key), row.value); }
	}
	return values;
}

/**
 * The keys whose value changed between the baseline and the new snapshot.
 *
 * With no baseline (the first stop) nothing changed. A key present only in the
 * new snapshot is not a change: there is nothing it could have changed from.
 */
export function changedKeys(previous: PanelSnapshot | null, next: PanelSnapshot): Set<string> {
	const changed = new Set<string>();
	if (previous === null) {
		return changed;
	}
	const before = cellValues(previous);
	for (const [key, value] of cellValues(next)) {
		const old = before.get(key);
		if (old !== undefined && old !== value) {
			changed.add(key);
		}
	}
	return changed;
}
