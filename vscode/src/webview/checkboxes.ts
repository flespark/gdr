/**
 * The panel's two-level checkbox tree: which tables are drawn, and which
 * columns of each table are shown.
 *
 * Parents are the tables (one per kind, flat). A child is one column of that
 * table, grouped as basic or diagnostic by the adapter's column contract. A
 * parent whose data is absent -- the heap with the heap switched off, or a
 * kind the target cannot enumerate -- is greyed out: it cannot be ticked
 * because there is nothing to draw. The tree is pure: it reads the snapshot
 * and the saved layout, and returns the state the markup shows.
 */

import type { ObjectTableData, PanelSnapshot } from '../snapshot.ts';
import type { PanelLayout } from './messages.ts';
/** Tables the panel draws, in their default order. Owned here: the checkbox tree is the list's first reader. */
export const TABLE_KINDS: readonly string[] = [
	'task',
	'timer',
	'heap',
	'semaphore',
	'mutex',
	'event',
	'mailbox',
	'msgqueue',
	'mempool',
	'queue',
	'eventgroup',
	'streambuffer'
];

export interface ColumnNode {
	/** The column's header, also its key in the layout. */
	key: string;
	group: 'basic' | 'diag';
	checked: boolean;
}

export interface TableNode {
	kind: string;
	title: string;
	/** Whether the table has data to draw; an unavailable table cannot be ticked. */
	available: boolean;
	/** Why the table is unavailable, shown beside the greyed box. */
	reason: string;
	checked: boolean;
	basic: ColumnNode[];
	diag: ColumnNode[];
	/** Actual display order, including interleaved diagnostic columns. */
	columns: ColumnNode[];
}

const TITLES: Record<string, string> = {
	task: 'Tasks',
	timer: 'Timers',
	heap: 'Heap',
	semaphore: 'Semaphores',
	mutex: 'Mutexes',
	event: 'Events',
	mailbox: 'Mailboxes',
	msgqueue: 'Message queues',
	mempool: 'Memory pools',
	queue: 'Queues',
	eventgroup: 'Event groups',
	streambuffer: 'Stream/message buffers'
};

/** The table a kind draws from in the snapshot, or null when it is not there. */
function tableOf(kind: string, snapshot: PanelSnapshot): ObjectTableData | null {
	if (kind === 'task') {
		return snapshot.data.tasks;
	}
	if (kind === 'heap') {
		return null;
	}
	return snapshot.data.tables?.[kind] ?? null;
}

/**
 * Why a parent cannot be ticked, or '' when it can.
 *
 * The heap is unavailable when it was not collected; any other kind is
 * unavailable when the capability says the target cannot enumerate it, or when
 * the snapshot carries no table for it.
 */
function unavailableReason(kind: string, snapshot: PanelSnapshot): string {
	if (kind === 'heap') {
		if (snapshot.data.heap === null) {
			return snapshot.noHeap ? 'heap collection is off' : 'the target has no heap';
		}
		return '';
	}
	const claim = snapshot.data.capabilities?.kinds[kind];
	if (claim !== undefined && !claim.enumerable) {
		return claim.detail !== '' ? claim.detail : 'the target cannot enumerate this kind';
	}
	if (tableOf(kind, snapshot) === null) {
		return snapshot.noDetails ? 'detail tables are off' : 'the target has none of this kind';
	}
	return '';
}

/** The columns of a table, split by group, each with its checked state. */
function columnsOf(kind: string, snapshot: PanelSnapshot, layout: PanelLayout | undefined): {
	basic: ColumnNode[];
	diag: ColumnNode[];
} {
	const table = kind === 'heap' ? null : tableOf(kind, snapshot);
	const hidden = new Set(layout?.tables[kind]?.hiddenColumns ?? []);
	const basic: ColumnNode[] = [];
	const diag: ColumnNode[] = [];
	if (kind === 'heap') {
		for (const [key] of snapshot.data.heap?.pairs ?? []) {
			if (key !== undefined) { basic.push({ key, group: 'basic', checked: !hidden.has(key) }); }
		}
		for (const section of snapshot.data.heap?.sections ?? []) {
			for (const row of section.rows) {
				diag.push({ key: row.key, group: 'diag', checked: !hidden.has(row.key) });
			}
		}
		return { basic, diag };
	}
	if (table === null) {
		return { basic, diag };
	}
	table.headers.forEach((header, index) => {
		const group = table.columns?.[index]?.group === 'diag' ? 'diag' : 'basic';
		const node: ColumnNode = { key: header, group, checked: !hidden.has(header) };
		(group === 'diag' ? diag : basic).push(node);
	});
	return { basic, diag };
}

/**
 * The whole tree, one node per table kind, in the panel's default order.
 *
 * Saved table and column orders are applied here and in the renderer. Hidden
 * columns remain selectable in their saved positions; diagnostics keep badges.
 */
export function buildTableTree(snapshot: PanelSnapshot, layout: PanelLayout | undefined): TableNode[] {
	const hiddenTables = new Set(layout?.hiddenTables ?? []);
	const saved = [...new Set((layout?.order ?? []).filter((kind) => TABLE_KINDS.includes(kind)))];
	const order = [...saved, ...TABLE_KINDS.filter((kind) => !saved.includes(kind))];
	return order.map((kind) => {
		const reason = unavailableReason(kind, snapshot);
		const { basic, diag } = columnsOf(kind, snapshot, layout);
		const all = [...basic, ...diag];
		const defaultKeys = kind === 'heap' ? all.map((column) => column.key) : tableOf(kind, snapshot)?.headers ?? [];
		const keys = [...new Set([...(layout?.tables[kind]?.columnOrder ?? []), ...defaultKeys])];
		const ordered = keys.flatMap((key) => all.filter((column) => column.key === key));
		// Reason: heap properties and checks render in two separate tables.
		const columns = kind === 'heap' ? [...ordered.filter((column) => column.group === 'basic'), ...ordered.filter((column) => column.group === 'diag')] : ordered;
		return {
			kind,
			title: TITLES[kind] ?? kind,
			available: reason === '',
			reason,
			checked: !hiddenTables.has(kind),
			basic,
			diag,
			columns
		};
	});
}

/** The layout the tree describes, given the reader's new tick state. */
export function toggleTable(layout: PanelLayout, kind: string, checked: boolean): PanelLayout {
	const hidden = new Set(layout.hiddenTables);
	if (checked) {
		hidden.delete(kind);
	} else {
		hidden.add(kind);
	}
	return { ...layout, hiddenTables: [...hidden] };
}

/** The layout with one column of one table ticked or unticked. */
export function toggleColumn(layout: PanelLayout, kind: string, column: string, checked: boolean): PanelLayout {
	const current = layout.tables[kind] ?? { kind, widths: {}, hiddenColumns: [] };
	const hidden = new Set(current.hiddenColumns);
	if (checked) {
		hidden.delete(column);
	} else {
		hidden.add(column);
	}
	return {
		...layout,
		tables: { ...layout.tables, [kind]: { ...current, hiddenColumns: [...hidden] } }
	};
}
