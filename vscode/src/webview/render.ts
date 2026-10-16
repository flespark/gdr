/**
 * Panel rendering: a GDR snapshot rendered as HTML, purely.
 *
 * Rendering happens host-side and the finished HTML crosses to the webview,
 * which only wires clicks, drags and the heap canvas. That keeps every rule in
 * one testable pure function: column alignment follows the adapter's
 * `ColumnSpec`, addresses become navigation anchors, waiter lists collapse,
 * ratios are shown as bars, verdicts are coloured, and explicit build advice
 * becomes a bounded hint block -- nothing here decodes kernel state.
 *
 * Layout is data too: the table order, hidden tables and hidden columns, and
 * the widths the reader set arrive as a `PanelLayout` and are written into the
 * markup. Widths travel as `data-width` attributes, not `style=`, because the
 * panel's CSP refuses inline styles; the client applies them after a render.
 */

import { escapeHtml } from './html.ts';

import { TABLE_KINDS, buildTableTree, type TableNode } from './checkboxes.ts';
import type { PanelLayout } from './messages.ts';
import type { PanelState } from './panelState.ts';
import type {
	ColumnSpec,
	HeapData,
	ObjectTableData,
	PanelSnapshot,
	SourceRef
} from '../snapshot.ts';
/** One adapter-owned actionable build configuration hint. */
export interface Hint {
	text: string;
	kind: string;
}

/** Only explicit adapter-owned build advice belongs in the message block. */
export function snapshotHints(snapshot: PanelSnapshot): Hint[] {
	return [...new Set(snapshot.data.buildHints ?? [])]
		.filter((text) => text.trim() !== '')
		.map((text) => ({ text, kind: 'info' }));
}

/** Human titles for the table cards. */
const TABLE_TITLES: Record<string, string> = {
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

/** A table as the panel draws it: a kind, its cells and the optional layout. */
export interface RenderOptions {
	/** Table kinds, in the order to draw. Defaults to TABLE_KINDS. */
	layout?: PanelLayout;
	/** Row identities whose value changed since the previous stop: `kind|id|column`. */
	changed?: ReadonlySet<string>;
	/** Heap dump is shown in the heap card once requested, keyed by session. */
	heapCanvas?: boolean;
}

/**
 * The complete panel: a tab strip of sessions and the active session's body.
 *
 * Pure by construction -- the caller supplies the state, this returns the
 * markup. The client script only swaps `innerHTML` and posts clicks back.
 */
export function renderApp(state: PanelState, options: RenderOptions = {}): string {
	const tabs = state.sessions
		.map((session) => {
			const dot = session.status === 'running' ? 'gdr-running' : `gdr-${session.status}`;
			const active = session.id === state.activeId ? ' class="gdr-tab gdr-active"' : ' class="gdr-tab"';
			return (
				`<button type="button"${active} data-session="${escapeHtml(session.id)}">` +
				`<span class="gdr-dot ${dot}"></span>` +
				`<span class="gdr-tab-name">${escapeHtml(session.name)}</span>` +
				`</button>`
			);
		})
		.join('');

	const active = state.sessions.find((session) => session.id === state.activeId) ?? null;
	const nav = `<nav class="gdr-tabs" aria-label="Debug sessions">${tabs}</nav>`;
	const body = active === null ? renderEmpty() : renderSession(active, options, nav);
	// Reason: the client keeps the body's scroll position across a refresh, but
	// only for the session it was showing; the tag tells it which one that is.
	const session = active === null ? '' : ` data-session="${escapeHtml(active.id)}"`;

	return (
		`<div class="gdr-panel">` +
		`<main class="gdr-body"${session}>${body}</main>` +
		`</div>`
	);
}

function renderEmpty(): string {
	return (
		`<div class="gdr-empty">` +
		`Start a debug session to see RTOS views — GDR loads when the session launches.` +
		`</div>`
	);
}

function renderSession(session: PanelState['sessions'][number], options: RenderOptions, tabs: string): string {
	if (session.status === 'error') {
		return tabs + renderStatus(`<h2>GDR could not initialize</h2><p>${escapeHtml(session.message ?? '')}</p>`, 'gdr-error');
	}
	if (session.status === 'waiting') {
		return tabs + renderStatus(`<h2>Waiting for the first stop</h2><p>${escapeHtml(session.message ?? '')}</p>`, 'gdr-waiting');
	}
	const snapshot = session.snapshot;
	if (snapshot === null || snapshot === undefined) {
		return tabs + renderStatus(`<h2>No snapshot yet</h2>`, 'gdr-waiting');
	}
	const stale = session.status === 'running' ? renderRunningBanner() : '';
	return stale + renderSnapshot(snapshot, options, tabs);
}

function renderStatus(inner: string, className: string): string {
	return `<div class="gdr-status-block ${className}">${inner}</div>`;
}

function renderRunningBanner(): string {
	return `<div class="gdr-running-banner">target running — showing the last stop</div>`;
}

// ---------------------------------------------------------------------------
// One snapshot
// ---------------------------------------------------------------------------

function renderSnapshot(snapshot: PanelSnapshot, options: RenderOptions, tabs: string): string {
	const meta = snapshotMetaLine(snapshot);
	const identity =
		snapshot.rtos !== null || snapshot.rtosVersion !== null
			? `${snapshot.rtos ?? 'unknown'} ${snapshot.rtosVersion ?? ''}`.trim()
			: 'GDR';
	const system = snapshot.data.system !== null ? renderSystem(snapshot.data.system.pairs, options) : '';
	const hints = renderHints(snapshot);
	const tree = renderCheckboxTree(buildTableTree(snapshot, options.layout));
	const cards = renderCards(snapshot, options);
	return (
		`<header class="gdr-session-header">` +
		`<span class="gdr-identity">${escapeHtml(identity)}</span>` +
		`${tabs}${meta}` +
		`</header>` +
		`${system}${hints}${tree}${cards}`
	);
}

/** Every table card, in the layout's order, minus the ones the reader hid. */
function renderCards(snapshot: PanelSnapshot, options: RenderOptions): string {
	const order = orderedKinds(options.layout);
	const hiddenTables = new Set(options.layout?.hiddenTables ?? []);
	return order
		.filter((kind) => !hiddenTables.has(kind))
		.map((kind) => renderCard(kind, snapshot, options))
		.join('');
}

/** The kinds in the reader's order: their saved order first, then the rest by default. */
export function orderedKinds(layout: PanelLayout | undefined): string[] {
	const saved = [...new Set((layout?.order ?? []).filter((kind) => TABLE_KINDS.includes(kind)))];
	const rest = TABLE_KINDS.filter((kind) => !saved.includes(kind));
	return [...saved, ...rest];
}

function renderCard(kind: string, snapshot: PanelSnapshot, options: RenderOptions): string {
	if (kind === 'heap') {
		return renderHeap(snapshot, options);
	}
	const table = tableFor(kind, snapshot);
	if (table === null) {
		return '';
	}
	return renderTableCard(TABLE_TITLES[kind] ?? kind, table, kind, options);
}

/** The table a kind draws from: tasks have their own; the rest come from `tables`. */
function tableFor(kind: string, snapshot: PanelSnapshot): ObjectTableData | null {
	if (kind === 'task') {
		return snapshot.data.tasks;
	}
	return snapshot.data.tables?.[kind] ?? null;
}

function snapshotMetaLine(snapshot: PanelSnapshot): string {
	const parts: string[] = [];
	if (snapshot.generation !== null) {
		parts.push(`gen ${snapshot.generation}`);
	}
	if (snapshot.elapsedMs !== null) {
		parts.push(`${snapshot.elapsedMs.toFixed(0)} ms`);
	}
	if (snapshot.collectedAt > 0) {
		parts.push(new Date(snapshot.collectedAt).toLocaleTimeString());
	}
	if (snapshot.noHeap) {
		parts.push('heap off');
	}
	if (snapshot.noDetails) {
		parts.push('details off');
	}
	return `<span class="gdr-meta">${parts.map(escapeHtml).join(' · ')}</span>`;
}

// ---------------------------------------------------------------------------
// Bounded build advice block (runtime faults use host popups)
// ---------------------------------------------------------------------------

function renderHints(snapshot: PanelSnapshot): string {
	const hints = snapshotHints(snapshot);
	if (hints.length === 0) {
		return '';
	}
	const items = hints
		.map((hint) => `<p class="gdr-hint gdr-hint-${hint.kind}">${escapeHtml(hint.text)}</p>`)
		.join('');
	return `<section class="gdr-hints" aria-label="RTOS build configuration hints">` +
		`<div class="gdr-hints-scroll" role="log" aria-live="polite" aria-relevant="additions">${items}</div>` +
		`<div class="gdr-hints-grip" role="separator" tabindex="0" aria-label="Resize build hints" aria-orientation="horizontal" aria-valuemin="48" aria-valuemax="480" aria-valuenow="88" title="Drag down to expand; use arrow keys to resize"><span aria-hidden="true">⌄</span></div></section>`;
}

// ---------------------------------------------------------------------------
// System: the task table's shape -- one header row, one value row
// ---------------------------------------------------------------------------

/**
 * The system summary as a header row and a value row, like the task table.
 *
 * The keys are the column headers and the values the cells, so a wide summary
 * scrolls sideways in its card instead of wrapping into a list.
 */
export function renderSystem(pairs: string[][], options: RenderOptions = {}): string {
	// Reason: object counts and heap counters already have dedicated tables.
	const kinds = new Set(['device', 'event', 'mailbox', 'memheap', 'mempool', 'msgqueue', 'mutex', 'semaphore', 'task', 'timer', 'task (walked)', 'Heap used', 'Heap total', 'Heap status']);
	const shown = pairs.filter(([key, value]) => key !== undefined && value !== undefined && !kinds.has(key));
	if (shown.length === 0) {
		return '';
	}
	const head = shown.map(([key]) => `<th>${escapeHtml(key ?? '')}</th>`).join('');
	const body = shown.map(([key, value]) => `<td${classAttr([options.changed?.has(changeKey('system', '', key ?? '')) ? 'gdr-changed' : ''])}>${escapeHtml(value ?? '')}</td>`).join('');
	return (
		`<section class="gdr-card gdr-system-card">` +
		`<h2>System</h2>` +
		`<div class="gdr-scroll">` +
		`<table class="gdr-table gdr-system-table"><thead><tr>${head}</tr></thead>` +
		`<tbody><tr>${body}</tr></tbody></table>` +
		`</div>` +
		`</section>`
	);
}

// ---------------------------------------------------------------------------
// Tables (tasks, per-kind details)
// ---------------------------------------------------------------------------

/** The stack size a usage column is a fraction of. */
const STACK_COLUMN = 'Stack';
/** Usage columns drawn as a fraction of the stack: current use and high water. */
const STACK_FRACTION_COLUMNS = ['Used', 'HighWater'];

/**
 * A ` class="…"` attribute for the non-empty class names, or nothing.
 *
 * Merges alignment and bar classes into one attribute, so a right-aligned bar
 * column does not end up with two `class` attributes on one tag.
 */
function classAttr(classes: string[]): string {
	const names = classes.filter((name) => name !== '');
	return names.length > 0 ? ` class="${names.join(' ')}"` : '';
}

/** Whether a column is drawn as a stack fraction (needs the stack column too). */
function isStackFraction(table: ObjectTableData, index: number): boolean {
	return STACK_FRACTION_COLUMNS.includes(table.headers[index] ?? '') && table.headers.includes(STACK_COLUMN);
}

/** The reader's hidden columns for one kind, if any. */
function hiddenColumnsOf(kind: string, options: RenderOptions): Set<string> {
	return new Set(options.layout?.tables[kind]?.hiddenColumns ?? []);
}

/** The column indexes to draw: every column except the reader's hidden ones. */
function shownIndexes(table: ObjectTableData, hidden: ReadonlySet<string>, order: readonly string[] = []): number[] {
	const keys = [...new Set([...order, ...table.headers])];
	return keys.map((key) => table.headers.indexOf(key)).filter((index) => index >= 0 && !hidden.has(table.headers[index] ?? ''));
}

/**
 * Whether a column is a verdict (diagnostic) column.
 *
 * The group comes from the adapter's ColumnSpec; a table without specs reads
 * as all basic, so the checkboxes simply offer no diagnostic children.
 */
export function isDiagColumn(table: ObjectTableData, index: number): boolean {
	return table.columns?.[index]?.group === 'diag';
}

function renderTableCard(
	title: string,
	table: ObjectTableData,
	kind: string,
	options: RenderOptions
): string {
	const hidden = hiddenColumnsOf(kind, options);
	const indexes = shownIndexes(table, hidden, options.layout?.tables[kind]?.columnOrder);
	const widths = options.layout?.tables[kind]?.widths ?? {};
	const head = indexes
		.map((index) => {
			const header = table.headers[index] ?? '';
			const spec = table.columns?.[index];
			const doc = spec?.doc !== undefined && spec.doc !== '' ? ` title="${escapeHtml(spec.doc)}"` : '';
			// Reason: every header reads left-aligned, whatever its column's data
			// alignment; only the data cells below are right-aligned.
			const bar = isStackFraction(table, index) ? 'gdr-bar-cell' : '';
			const diag = isDiagColumn(table, index) ? 'gdr-diag-col' : '';
			// Reason: widths are data, not style: the client applies them after the
			// render because the CSP refuses inline style attributes.
			const width = widths[header] !== undefined ? ` data-width="${widths[header]}"` : '';
			return `<th${classAttr([bar, diag])} data-col="${escapeHtml(header)}"${width}${doc} draggable="true">${escapeHtml(header)}<span class="gdr-col-grip" aria-hidden="true"></span></th>`;
		})
		.join('');
	const body = renderTableRows(table, kind, indexes, options);
	return (
		`<section class="gdr-card" data-kind="${escapeHtml(kind)}" data-column-order="${escapeHtml(JSON.stringify(options.layout?.tables[kind]?.columnOrder ?? table.headers))}">` +
		`<h2 class="gdr-card-title" draggable="true" title="Drag to reorder tables">${escapeHtml(title)}</h2>` +
		`<div class="gdr-scroll">` +
		`<table class="gdr-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>` +
		`</div>` +
		`</section>`
	);
}

function renderTableRows(
	table: ObjectTableData,
	kind: string,
	indexes: readonly number[],
	options: RenderOptions
): string {
	const stackIndex = table.headers.indexOf(STACK_COLUMN);
	return table.rows
		.map((row, rowIndex) => {
			const values = table.value_rows?.[rowIndex] ?? [];
			const sources = table.source_rows?.[rowIndex] ?? [];
			const id = String(table.ids?.[rowIndex] ?? '');
			const cells = indexes
				.map((columnIndex): string => {
					const text = row[columnIndex] ?? '';
					const spec: ColumnSpec | undefined = table.columns?.[columnIndex];
					const align = spec?.align === 'right' ? 'gdr-right' : '';
					const changed = options.changed?.has(changeKey(kind, id, table.headers[columnIndex] ?? ''))
						? 'gdr-changed'
						: '';
					if (spec?.kind === 'addr') {
						const address = addressLiteral(values[columnIndex]);
						return `<td${classAttr([align, 'gdr-addr', changed])}>${address === undefined ? escapeHtml(text) : renderAddress(text, address)}</td>`;
					}
					if (spec?.kind === 'waiters') {
						return `<td${classAttr([align, 'gdr-waiters', changed])}>${renderWaiters(text, values[columnIndex], changeKey(kind, id, table.headers[columnIndex] ?? ''))}</td>`;
					}
					if (spec?.kind === 'addresses' && Array.isArray(values[columnIndex])) {
						return `<td${classAttr([align, changed])}>${renderAddressList(values[columnIndex] as unknown[])}</td>`;
					}
					const source = sources[columnIndex];
					if (source !== undefined && source !== null) {
						// Reason: a PC or Entry cell is a source link when GDR resolved a line;
						// the text stays the symbol the table shows.
						return `<td${classAttr([align, changed])}>${renderSourceLink(text, source, addressLiteral(values[columnIndex]))}</td>`;
					}
					if ((spec?.kind === 'pc' || spec?.kind === 'symbol') && addressLiteral(values[columnIndex]) !== undefined) {
						// Reason: a PC with no source line is still an address to inspect.
						return `<td${classAttr([align, 'gdr-addr', changed])}>${renderAddress(text, addressLiteral(values[columnIndex]))}</td>`;
					}
					if (stackIndex >= 0 && isStackFraction(table, columnIndex)) {
						const percent = stackPercent(numeric(values[columnIndex]), numeric(values[stackIndex]));
						if (percent !== null) {
							// Reason: the bar is a background class picked here, not an inline
							// style: the panel's CSP refuses style attributes outright.
							const hot = percent >= 90 ? 'gdr-pct-hot' : '';
							const cls = classAttr([align, 'gdr-bar-cell', `gdr-pct-${percent}`, hot, changed]);
							return `<td${cls} title="${percent}% of the stack">${escapeHtml(text)}</td>`;
						}
					}
					return `<td${classAttr([align, changed])}>${escapeHtml(text)}</td>`;
				})
				.join('');
			return `<tr data-id="${escapeHtml(id)}">${cells}</tr>`;
		})
		.join('');
}

/**
 * The key a changed cell is recorded under: table kind, row identity, column.
 *
 * The same key is built by the host's diff (see `diff.ts`), so a highlight
 * always names the cell that changed.
 */
export function changeKey(kind: string, rowId: string, column: string): string {
	return `${kind}|${rowId}|${column}`;
}

/** An address cell turns into a click target the webview wires to the host. */
export function renderAddress(text: string, address: string = text): string {
	return `<a href="#" data-address="${escapeHtml(address)}" data-pointer="${escapeHtml(address)}" aria-label="${escapeHtml(text)} (${escapeHtml(address)})">${escapeHtml(text)}</a>`;
}

function addressLiteral(value: unknown): string | undefined {
	if (typeof value === 'number' && Number.isSafeInteger(value) && value > 0) { return `0x${value.toString(16)}`; }
	return typeof value === 'string' && /^0x[0-9a-f]+$/i.test(value) ? value : undefined;
}

function renderAddressList(entries: unknown[]): string {
	return entries.map((entry) => {
		if (typeof entry !== 'object' || entry === null) { return ''; }
		const item = entry as { text?: unknown; address?: unknown };
		const text = typeof item.text === 'string' ? item.text : '';
		const address = addressLiteral(item.address);
		return address === undefined ? escapeHtml(text) : renderAddress(text, address);
	}).join('<br>');
}

/** The count stays compact; every known waiter is available in the overlay. */
export function renderWaiters(text: string, value: unknown, key: string = ''): string {
	if (!Array.isArray(value)) { return escapeHtml(text); }
	if (value.length === 0) { return '0'; }
	const items = value.map((entry) => {
		if (typeof entry === 'string') { return `<li>${escapeHtml(entry)}</li>`; }
		if (typeof entry !== 'object' || entry === null) { return ''; }
		const waiter = entry as { name?: string; source?: SourceRef | null; address?: unknown };
		const name = waiter.name ?? '';
		const address = addressLiteral(waiter.address);
		return `<li>${waiter.source ? renderSourceLink(name, waiter.source, address) : address !== undefined ? renderAddress(name, address) : escapeHtml(name)}</li>`;
	}).join('');
	return `<details class="gdr-waiter-fold" data-disclosure="waiters:${escapeHtml(key)}"><summary>${value.length}</summary><ul class="gdr-waiter-menu">${items}</ul></details>`;
}

/**
 * A usage value as a whole percent of its stack, clamped to 0..100.
 *
 * Presentation arithmetic only (the same ratio the stack column implies);
 * `null` when either side is missing or the stack is empty.
 */
export function stackPercent(used: number | null, stack: number | null): number | null {
	if (used === null || stack === null || stack <= 0) {
		return null;
	}
	return Math.max(0, Math.min(100, Math.round((used / stack) * 100)));
}

/**
 * A symbol cell whose code has a known source line: a link the webview turns
 * into an "open this line" request. The text stays the symbol the table shows.
 *
 * The name carries the path as the debug information recorded it, so the host
 * can resolve a build directory that is absent here (see `resolveSourceFile`).
 */
export function renderSourceLink(text: string, source: SourceRef, address?: string): string {
	const where = `${source.file}:${source.line}`;
	return (
		`<a href="#" class="gdr-source" data-source-file="${escapeHtml(source.file)}" ` +
		`data-source-name="${escapeHtml(source.name)}" ` +
		`data-source-line="${source.line}"${address !== undefined ? ` data-fallback-address="${escapeHtml(address)}" data-pointer="${escapeHtml(address)}" aria-label="${escapeHtml(text)} (${escapeHtml(address)})"` : ` title="${escapeHtml(where)}"`}>${escapeHtml(text)}</a>`
	);
}

function numeric(value: unknown): number | null {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

// ---------------------------------------------------------------------------
// Checkbox tree: which tables are drawn, and which columns of each table
// ---------------------------------------------------------------------------

/**
 * The two-level checkbox list above the cards.
 *
 * Each parent is one table; its children follow the actual column order,
 * with diagnostic badges kept on their columns. An unavailable parent is greyed out and
 * cannot be ticked. The markup carries `data-table` and `data-column` so the
 * client can post a tick back as a layout change.
 */
export function renderCheckboxTree(tree: TableNode[]): string {
	const parents = tree
		.map((node) => {
			const disabled = node.available ? '' : ' disabled';
			const checked = node.checked && node.available ? ' checked' : '';
			const reason = node.available ? '' : ` <span class="gdr-tree-reason">${escapeHtml(node.reason)}</span>`;
			const children = node.columns
				.map(
					(column) =>
						`<li class="gdr-tree-child gdr-tree-${column.group}" data-tree-column="${escapeHtml(column.key)}" draggable="true" title="Drag to reorder columns">` +
						`<label><input type="checkbox" data-table="${escapeHtml(node.kind)}" ` +
						`data-column="${escapeHtml(column.key)}"${column.checked ? ' checked' : ''}${disabled}/> ` +
						`<span class="gdr-drag-handle" aria-hidden="true">⠿</span><span>${escapeHtml(column.key)}</span>${column.group === 'diag' ? '<span class="gdr-tree-badge">check</span>' : ''}</label></li>`
				)
				.join('');
			return (
				`<li data-tree-kind="${escapeHtml(node.kind)}" class="gdr-tree-parent${node.available ? '' : ' gdr-tree-unavailable'}">` +
				`<details data-disclosure="columns:${escapeHtml(node.kind)}"><summary draggable="true" title="Drag to reorder tables"><span class="gdr-drag-handle" aria-hidden="true">⠿</span><label><input type="checkbox" data-table="${escapeHtml(node.kind)}"${checked}${disabled}/> ` +
				`<span>${escapeHtml(node.title)}</span></label><span class="gdr-tree-count">${node.columns.filter((column) => column.checked).length}/${node.columns.length}</span></summary>${reason}` +
				(children !== '' ? `<ul class="gdr-tree-children">${children}</ul>` : '') +
				`</details></li>`
			);
		})
		.join('');
	return `<details class="gdr-tree" data-disclosure="tables"><summary>Display selection<span class="gdr-tree-badge">drag to reorder</span></summary><ul class="gdr-tree-root">${parents}</ul></details>`;
}

// ---------------------------------------------------------------------------
// Heap: two rows of basics, then the verdict columns
// ---------------------------------------------------------------------------

/**
 * The heap card: basics and verdict rows as one header row and one value
 * row, the block count included. With the heap switched off the card says
 * so; a heap-dump button asks for the bitmap on demand.
 */
function renderHeap(snapshot: PanelSnapshot, options: RenderOptions): string {
	const heap = snapshot.data.heap;
	if (heap === null) {
		if (snapshot.noHeap) {
			return (
				`<section class="gdr-card gdr-card-off" data-kind="heap"><h2>Heap</h2>` +
				`<p class="gdr-hint gdr-hint-info">hidden by the heap cost switch — enable gdr.refreshHeaps to collect it</p>` +
				`</section>`
			);
		}
		return '';
	}
	return (
		`<section class="gdr-card" data-kind="heap">` +
		`<h2 class="gdr-card-title" draggable="true" title="Drag to reorder tables">Heap</h2>` +
		renderHeapTable(heap, options) +
		renderHeapDump(options) +
		`</section>`
	);
}

/**
 * The basics plus the verdict rows as one header row and one value row,
 * like the system summary with its checks inline. Basics keep their
 * position ahead of the checks; a saved column order applies across both.
 */
function renderHeapTable(heap: HeapData, options: RenderOptions): string {
	const order = options.layout?.tables.heap?.columnOrder ?? [];
	const hidden = hiddenColumnsOf('heap', options);
	const shown = heap.pairs.filter(([key, value]) => key !== undefined && value !== undefined && !hidden.has(key))
		.sort(([a], [b]) => (order.indexOf(a ?? '') < 0 ? order.length : order.indexOf(a ?? '')) - (order.indexOf(b ?? '') < 0 ? order.length : order.indexOf(b ?? '')));
	const verdicts = heap.sections.flatMap((section) => section.rows).filter((row) => !hidden.has(row.key))
		.sort((a, b) => (order.indexOf(a.key) < 0 ? order.length : order.indexOf(a.key)) - (order.indexOf(b.key) < 0 ? order.length : order.indexOf(b.key)));
	if (shown.length === 0 && verdicts.length === 0) {
		return '';
	}
	const widths = options.layout?.tables.heap?.widths ?? {};
	const head = shown.map(([key]) => `<th data-col="${escapeHtml(key ?? '')}"${widths[key ?? ''] !== undefined ? ` data-width="${widths[key ?? '']}"` : ''} draggable="true">${escapeHtml(key ?? '')}<span class="gdr-col-grip" aria-hidden="true"></span></th>`).join('') +
		verdicts.map((row) => `<th class="gdr-diag-col" data-col="${escapeHtml(row.key)}"${widths[row.key] !== undefined ? ` data-width="${widths[row.key]}"` : ''} draggable="true">${escapeHtml(row.key)}<span class="gdr-col-grip" aria-hidden="true"></span></th>`).join('');
	const body = shown.map(([key, value]) => `<td${classAttr([options.changed?.has(changeKey('heap', '', key ?? '')) ? 'gdr-changed' : ''])}>${escapeHtml(value ?? '')}</td>`).join('') +
		verdicts.map((row) =>
			`<td class="gdr-diag-col gdr-verdict gdr-verdict-${escapeHtml(row.severity)}${options.changed?.has(changeKey('heap', '', row.key)) ? ' gdr-changed' : ''}">` +
			`${escapeHtml(row.value)}</td>`
		).join('');
	// Reason: runtime/allocator messages are not actionable build advice.

	return (
		`<div class="gdr-scroll">` +
		`<table class="gdr-table gdr-heap-table"><thead><tr>${head}</tr></thead>` +
		`<tbody><tr>${body}</tr></tbody></table>` +
		`</div>`
	);
}

/**
 * The on-demand heap bitmap keeps one section shape: the details shell is
 * always present with a bare summary, and expanding it requests the draw
 * when no bitmap is there yet. The labels and the legend are shells the
 * client fills after the heap map arrives; the markup carries no inline
 * handlers for the CSP.
 */
function renderHeapDump(options: RenderOptions): string {
	// Reason: the map stays folded until the reader opens it; the client
	// disclosure memory keeps the choice per session, so a stop refresh
	// never springs it back open. Expanding still requests the draw.
	const requested = options.heapCanvas === true;
	const badge = requested ? ` <span class="gdr-tree-badge">hover for block details</span>` : '';
	const body = !requested
		? ``
		: `<div class="gdr-heap-addr" data-heap-addr-start="1"></div>` +
			`<div class="gdr-heap-body">` +
			`<div class="gdr-heap-map"><canvas class="gdr-heap-canvas" data-heap-canvas="1" draggable="false"></canvas></div>` +
			`<div class="gdr-heap-side">` +
			`<div class="gdr-heap-grip" data-heap-grip="1"></div>` +
			`<div class="gdr-heap-corner" data-heap-corner="1"></div>` +
			`</div>` +
			`<div class="gdr-heap-legend" data-heap-legend="1"></div>` +
			`</div>` +
			`<div class="gdr-heap-addr" data-heap-addr-end="1"></div>`;
	return (
		`<details class="gdr-heap-fold" data-disclosure="heap-map">` +
		`<summary>Heap map${badge}</summary>` +
		`<div class="gdr-heap-view">${body}</div>` +
		`<div class="gdr-heap-tooltip" data-heap-tooltip="1" role="tooltip" hidden></div>` +
		`<div class="gdr-heap-resize" data-heap-resize="1" hidden></div>` +
		`</details>`
	);
}

// ---------------------------------------------------------------------------
// The webview shell
// ---------------------------------------------------------------------------

export interface ShellArgs {
	cspSource: string;
	styleUri: string;
	clientUri: string;
}

/**
 * The webview document that hosts the rendered panel.
 *
 * No inline script or style: the CSP allows only the webview's own URIs, and
 * the client script is the compiled `webview/client.ts` (a zero-import file,
 * so the classic-script loader can run it without a bundler). Heap bitmaps are
 * `data:` images, the one extra source the CSP allows.
 */
export function renderShell(args: ShellArgs): string {
	return (
		`<!DOCTYPE html><html lang="en"><head>` +
		`<meta charset="utf-8" />` +
		`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${escapeHtml(args.cspSource)} data:; style-src ${escapeHtml(args.cspSource)}; script-src ${escapeHtml(args.cspSource)};" />` +
		`<link rel="stylesheet" href="${escapeHtml(args.styleUri)}" />` +
		`</head><body>` +
		`<div id="gdr-root"></div>` +
		`<script src="${escapeHtml(args.clientUri)}"></script>` +
		`</body></html>`
	);
}
