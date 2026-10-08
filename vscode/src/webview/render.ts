/**
 * Panel rendering: a GDR snapshot rendered as HTML, purely.
 *
 * Rendering happens host-side and the finished HTML crosses to the webview,
 * which only wires clicks back. That keeps every rule in one testable pure
 * function: column alignment follows the adapter's `ColumnSpec`, addresses
 * become navigation anchors, waiter lists collapse, ratios are shown as bars,
 * and capability notes become hint bars -- nothing here decodes kernel state.
 */

import { escapeHtml } from './html.ts';

import type { PanelState } from './panelState.ts';
import type { ColumnSpec, ObjectTableData, PanelSnapshot, SourceRef } from '../snapshot.ts';
import type { CheckFinding } from '../snapshot.ts';

/** A reference to the address cell anchor the webview turns into navigation. */
export interface RenderedPanel {
	html: string;
}

/**
 * The complete panel: a tab strip of sessions and the active session's body.
 *
 * Pure by construction -- the caller supplies the state, this returns the
 * markup. The client script only swaps `innerHTML` and posts clicks back.
 */
export function renderApp(state: PanelState): string {
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
	const body = active === null ? renderEmpty() : renderSession(active);
	// Reason: the client keeps the body's scroll position across a refresh, but
	// only for the session it was showing; the tag tells it which one that is.
	const session = active === null ? '' : ` data-session="${escapeHtml(active.id)}"`;

	return (
		`<div class="gdr-panel">` +
		`<nav class="gdr-tabs">${tabs}</nav>` +
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

function renderSession(session: PanelState['sessions'][number]): string {
	if (session.status === 'error') {
		return renderStatus(`<h2>GDR could not initialize</h2><p>${escapeHtml(session.message ?? '')}</p>`, 'gdr-error');
	}
	if (session.status === 'waiting') {
		return renderStatus(`<h2>Waiting for the first stop</h2><p>${escapeHtml(session.message ?? '')}</p>`, 'gdr-waiting');
	}
	const snapshot = session.snapshot;
	if (snapshot === null || snapshot === undefined) {
		return renderStatus(`<h2>No snapshot yet</h2>`, 'gdr-waiting');
	}
	const stale = session.status === 'running' ? renderRunningBanner() : '';
	return stale + renderSnapshot(snapshot);
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

function renderSnapshot(snapshot: PanelSnapshot): string {
	const meta = snapshotMetaLine(snapshot);
	const identity =
		snapshot.rtos !== null || snapshot.rtosVersion !== null
			? `${snapshot.rtos ?? 'unknown'} ${snapshot.rtosVersion ?? ''}`.trim()
			: 'GDR';
	const system = snapshot.data.system !== null ? renderSystem(snapshot.data.system.pairs) : '';
	const hints = renderHints(snapshot);
	const tasks = snapshot.data.tasks !== null ? renderTableCard('Tasks', snapshot.data.tasks) : '';
	const objects = snapshot.data.objects !== null ? renderTableCard('Objects', snapshot.data.objects) : '';
	const heap = renderHeap(snapshot);
	const checks = snapshot.data.checks !== null ? renderChecks(snapshot.data.checks.findings) : '';
	return (
		`<header class="gdr-session-header">` +
		`<span class="gdr-identity">${escapeHtml(identity)}</span>` +
		`${meta}` +
		`</header>` +
		`${system}${hints}${tasks}${objects}${checks}${heap}`
	);
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
	return `<span class="gdr-meta">${parts.map(escapeHtml).join(' · ')}</span>`;
}

// ---------------------------------------------------------------------------
// Hint bars: envelope warnings, table scan notes, capability gaps
// ---------------------------------------------------------------------------

/**
 * Collect every hint line a snapshot carries: envelope warnings, per-table
 * messages, and capability claims that name a missing configuration.
 */
export function snapshotHints(snapshot: PanelSnapshot): { text: string; kind: string }[] {
	const hints: { text: string; kind: string }[] = [];
	for (const warning of snapshot.warnings) {
		hints.push({ text: warning, kind: 'info' });
	}
	for (const message of snapshot.data.tasks?.messages ?? []) {
		hints.push({ text: message, kind: 'info' });
	}
	for (const message of snapshot.data.objects?.messages ?? []) {
		hints.push({ text: message, kind: 'info' });
	}
	for (const message of snapshot.data.heap?.messages ?? []) {
		hints.push({ text: message, kind: 'info' });
	}
	const capabilities = snapshot.data.capabilities;
	if (capabilities !== null) {
		for (const [kind, claim] of Object.entries(capabilities.kinds)) {
			let gap = claim.note;
			if (gap === '' && !claim.enumerable) {
				gap = claim.detail;
			}
			if (gap !== '') {
				hints.push({ text: `${kind}: ${gap}`, kind: 'warn' });
			}
		}
		for (const line of capabilities.extra) {
			hints.push({ text: line, kind: 'info' });
		}
	}
	return hints;
}

function renderHints(snapshot: PanelSnapshot): string {
	const hints = snapshotHints(snapshot);
	if (hints.length === 0) {
		return '';
	}
	const items = hints
		.map(
			(hint) =>
				`<p class="gdr-hint gdr-hint-${hint.kind}">${escapeHtml(hint.text)}</p>`
		)
		.join('');
	return `<div class="gdr-hints">${items}</div>`;
}

// ---------------------------------------------------------------------------
// System summary
// ---------------------------------------------------------------------------

function renderSystem(pairs: string[][]): string {
	const rows = pairs
		.slice(0, 8)
		.map(
			([key, value]) =>
				`<span class="gdr-sys-key">${escapeHtml(key)}</span>` +
				`<span class="gdr-sys-value">${escapeHtml(value)}</span>`
		)
		.join('');
	return rows === '' ? '' : `<dl class="gdr-system">${rows}</dl>`;
}

// ---------------------------------------------------------------------------
// Tables (tasks, objects, heap free list)
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
	return (
		STACK_FRACTION_COLUMNS.includes(table.headers[index] ?? '') &&
		table.headers.includes(STACK_COLUMN)
	);
}

function renderTableCard(title: string, table: ObjectTableData): string {
	const head = table.headers
		.map((header, index) => {
			const spec = table.columns?.[index];
			const doc = spec?.doc !== undefined && spec.doc !== '' ? ` title="${escapeHtml(spec.doc)}"` : '';
			// Reason: every header reads left-aligned, whatever its column's data
			// alignment; only the data cells below are right-aligned.
			const bar = isStackFraction(table, index) ? 'gdr-bar-cell' : '';
			return `<th${classAttr([bar])}${doc}>${escapeHtml(header)}</th>`;
		})
		.join('');
	const body = renderTableRows(table);
	return (
		`<section class="gdr-card">` +
		`<h2>${escapeHtml(title)}</h2>` +
		`<div class="gdr-scroll">` +
		`<table class="gdr-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>` +
		`</div>` +
		`</section>`
	);
}

function renderTableRows(table: ObjectTableData): string {
	const stackIndex = table.headers.indexOf(STACK_COLUMN);
	return table.rows
		.map((row, rowIndex) => {
			const values = table.value_rows?.[rowIndex] ?? [];
			const sources = table.source_rows?.[rowIndex] ?? [];
			const cells = row
				.map((text, columnIndex): string => {
					const spec: ColumnSpec | undefined = table.columns?.[columnIndex];
					const align = spec?.align === 'right' ? 'gdr-right' : '';
					if (spec?.kind === 'addr' && values[columnIndex] !== undefined) {
						return `<td${classAttr([align, 'gdr-addr'])}>${renderAddress(text)}</td>`;
					}
					if (spec?.kind === 'waiters') {
						return `<td${classAttr([align, 'gdr-waiters'])}>${renderWaiters(text, values[columnIndex])}</td>`;
					}
					const source = sources[columnIndex];
					if (source !== undefined && source !== null) {
						return `<td${classAttr([align])}>${renderSourceLink(text, source)}</td>`;
					}
					if (stackIndex >= 0 && isStackFraction(table, columnIndex)) {
						const percent = stackPercent(numeric(values[columnIndex]), numeric(values[stackIndex]));
						if (percent !== null) {
							// Reason: the bar is a background class picked here, not an inline
							// style: the panel's CSP refuses style attributes outright.
							const hot = percent >= 90 ? 'gdr-pct-hot' : '';
							const cls = classAttr([align, 'gdr-bar-cell', `gdr-pct-${percent}`, hot]);
							return `<td${cls} title="${percent}% of the stack">${escapeHtml(text)}</td>`;
						}
					}
					return `<td${classAttr([align])}>${escapeHtml(text)}</td>`;
				})
				.join('');
			return `<tr data-id="${escapeHtml(String(table.ids?.[rowIndex] ?? ''))}">${cells}</tr>`;
		})
		.join('');
}

/** An address cell turns into a click target the webview wires to the host. */
export function renderAddress(text: string): string {
	return `<a href="#" data-address="${escapeHtml(text)}">${escapeHtml(text)}</a>`;
}

/** Waiters collapse into a native disclosure when more than one name is known. */
export function renderWaiters(text: string, value: unknown): string {
	if (!Array.isArray(value)) {
		return escapeHtml(text);
	}
	const names = value.filter((name): name is string => typeof name === 'string');
	const visible = names.length >= 2 ? names.slice(0, 2).join(', ') : text;
	const summary = `${escapeHtml(visible)}${names.length > 2 ? ` (+${names.length - 2})` : ''}`;
	if (names.length <= 2) {
		return summary;
	}
	return (
		`<details class="gdr-waiter-fold">` +
		`<summary>${summary}</summary>` +
		`<ul>${names.map((name) => `<li>${escapeHtml(name)}</li>`).join('')}</ul>` +
		`</details>`
	);
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
 */
export function renderSourceLink(text: string, source: SourceRef): string {
	const where = `${source.file}:${source.line}`;
	return (
		`<a href="#" class="gdr-source" data-source-file="${escapeHtml(source.file)}" ` +
		`data-source-line="${source.line}" title="${escapeHtml(where)}">${escapeHtml(text)}</a>`
	);
}

function numeric(value: unknown): number | null {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

// ---------------------------------------------------------------------------
// Heap
// ---------------------------------------------------------------------------

function renderHeap(snapshot: PanelSnapshot): string {
	const heap = snapshot.data.heap;
	if (heap === null) {
		if (snapshot.noHeap) {
			return `<section class="gdr-card gdr-card-off"><h2>Heap</h2>` +
				`<p class="gdr-hint gdr-hint-info">hidden by the heap cost switch — enable gdr.refreshHeaps to collect it</p>` +
				`</section>`;
		}
		return '';
	}
	const pairs = heap.pairs
		.map(
			([key, value]) =>
				`<span class="gdr-sys-key">${escapeHtml(key)}</span>` +
				`<span class="gdr-sys-value">${escapeHtml(value)}</span>`
		)
		.join('');
	const table = heap.table !== null ? renderTableCard(heap.tableTitle, heap.table) : '';
	return `<section class="gdr-card"><h2>Heap</h2><dl class="gdr-system">${pairs}</dl>${table}</section>`;
}

// ---------------------------------------------------------------------------
// Checks
// ---------------------------------------------------------------------------

function renderChecks(findings: CheckFinding[]): string {
	if (findings.length === 0) {
		return '';
	}
	const items = findings
		.map(
			(finding) =>
				`<li class="gdr-verdict gdr-verdict-${escapeHtml(finding.verdict)}">` +
				`<b>${escapeHtml(finding.name)}</b>` +
				(finding.detail !== '' ? ` — ${escapeHtml(finding.detail)}` : '') +
				`</li>`
		)
		.join('');
	return `<section class="gdr-card"><h2>Checks</h2><ul class="gdr-checks">${items}</ul></section>`;
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
 * so the classic-script loader can run it without a bundler).
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
