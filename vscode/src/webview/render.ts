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
import type { ColumnSpec, ObjectTableData, PanelSnapshot } from '../snapshot.ts';
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

	return (
		`<div class="gdr-panel">` +
		`<nav class="gdr-tabs">${tabs}</nav>` +
		`<main class="gdr-body">${body}</main>` +
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
			const gap = claim.note !== '' ? claim.note : claim.enumerable ? '' : claim.detail;
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

function renderTableCard(title: string, table: ObjectTableData): string {
	const head = table.headers
		.map((header, index) => {
			const spec = table.columns?.[index];
			const doc = spec?.doc !== undefined && spec.doc !== '' ? ` title="${escapeHtml(spec.doc)}"` : '';
			const align = spec?.align === 'right' ? ' class="gdr-right"' : '';
			return `<th${align}${doc}>${escapeHtml(header)}</th>`;
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
	const usedIndex = table.headers.indexOf('Used');
	const stackIndex = table.headers.indexOf('Stack');
	const percent = usedIndex >= 0 && stackIndex >= 0;
	return table.rows
		.map((row, rowIndex) => {
			const values = table.value_rows?.[rowIndex] ?? [];
			const cells = row
				.map((text, columnIndex): string => {
					const spec: ColumnSpec | undefined = table.columns?.[columnIndex];
					const align = spec?.align === 'right' ? ' class="gdr-right"' : '';
					if (spec?.kind === 'addr' && values[columnIndex] !== undefined) {
						return `<td${align} class="gdr-addr">${renderAddress(text)}</td>`;
					}
					if (spec?.kind === 'waiters') {
						return `<td${align} class="gdr-waiters">${renderWaiters(text, values[columnIndex])}</td>`;
					}
					if (percent && columnIndex === usedIndex && spec?.kind === 'int') {
						const used = numeric(values[usedIndex]);
						const stack = numeric(values[stackIndex]);
						return `<td${align} class="gdr-used">${escapeHtml(text)}${renderPercentBar(used, stack)}</td>`;
					}
					return `<td${align}>${escapeHtml(text)}</td>`;
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
 * A used/stack ratio as a bar, purely presentational.
 *
 * The columns are the adapter's own numeric pairs; the division is display
 * arithmetic only, never a kernel read.
 */
export function renderPercentBar(used: number | null, stack: number | null): string {
	if (used === null || stack === null || stack <= 0) {
		return '';
	}
	const percent = Math.min(100, Math.round((used / stack) * 100));
	const cls = percent >= 90 ? 'gdr-bar-fill gdr-bar-hot' : 'gdr-bar-fill';
	return `<span class="gdr-bar" title="${percent}% used">` +
		`<span class="${cls}" style="width:${percent}%"></span></span>`;
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
	const table = heap.table !== null ? renderTableCard('Free blocks', heap.table) : '';
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
