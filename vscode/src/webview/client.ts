/**
 * The webview bootstrap: swap host-rendered HTML and wire clicks back.
 *
 * This file must stay import-free: the webview loads it as a classic script,
 * and the extension has no bundler yet, so it cannot `require` anything. The
 * host renders the whole panel (see `render.ts`) and posts the markup; the
 * only data flowing back is a click on an address or source line, or a
 * refresh request.
 */

/// <reference lib="dom" />
// Reason: the main tsconfig targets the extension host (lib ES2022 only);
// this file runs inside the webview where the DOM is the runtime, so it
// pulls the DOM lib in locally rather than loosening the whole project.

declare function acquireVsCodeApi(): {
	postMessage(message: unknown): void;
};

interface ClickMessage {
	type: 'openMemory' | 'openSource' | 'heapDump' | 'layout' | 'refresh' | 'selectSession';
	sessionId?: string;
	address?: string;
	file?: string;
	name?: string;
	line?: number;
	fallbackAddress?: string;
	layout?: ClientLayout;
}

/**
 * The layout the reader sees, as the host sends it and the client posts it back.
 *
 * Mirrors `PanelLayout` in messages.ts. This file is import-free (a classic
 * script), so the shape is restated here; the host decodes it at the boundary.
 */
interface ClientLayout {
	order: string[];
	hiddenTables: string[];
	tables: Record<string, { kind: string; widths: Record<string, number>; hiddenColumns: string[]; columnOrder?: string[] }>;
}

/** The scroll positions a host refresh would otherwise reset to the top. */
interface ScrollMemory {
	session: string | null;
	top: number;
	/** Horizontal offsets of the table scrollers, in document order. */
	tables: Record<string, number>;
}

const api = acquireVsCodeApi();

/** The heap block layout the host last sent; drawn on the canvas after each render. */
let heapMapData: ClientHeapMap | null = null;
let savedLayout: ClientLayout = { order: [], hiddenTables: [], tables: {} };
/** Per-session hint order/height survive host redraws, without mixing sessions. */
const hintMemory = new Map<string, { texts: string[]; height: number; top: number; seen: boolean }>();
const root = document.getElementById('gdr-root');
if (root === null) {
	throw new Error('gdr webview root is missing');
}

window.addEventListener('message', (event: MessageEvent) => {
	// Reason: the host is the only legitimate sender. VS Code relays it through
	// the parent frame that shares this webview's origin, so the VS Code team's
	// own guidance for webviews is an exact origin match (`e.origin ===
	// window.origin`); a foreign frame that got a handle on this window must
	// not be allowed to replace the panel's markup.
	if (event.origin !== window.origin) {
		return;
	}
	const message: unknown = event.data;
	if (typeof message !== 'object' || message === null) {
		return;
	}
	const payload = message as { type?: unknown; html?: unknown; map?: unknown; layout?: ClientLayout };
	if (payload.type === 'state' && typeof payload.html === 'string') {
		if (payload.layout !== undefined) { savedLayout = payload.layout; }
		if ('map' in payload) { heapMapData = isHeapMap(payload.map) ? payload.map : null; }
		applyTrustedHostMarkup(root, payload.html);
		return;
	}
	if (payload.type === 'heapMap' && isHeapMap(payload.map)) {
		const sessionId = (message as { sessionId?: string }).sessionId;
		if (sessionId !== bodyOf(root)?.dataset.session) { return; }
		heapMapData = payload.map;
		drawHeapCanvas(root);
	}
});

document.addEventListener('click', (event: MouseEvent) => {
	const target = event.target as HTMLElement | null;
	const tab = target?.closest<HTMLElement>('.gdr-tab');
	if (tab !== null && tab !== undefined) {
		api.postMessage({ type: 'selectSession', sessionId: tab.dataset.session });
		return;
	}
	if (closestData(target, 'data-heap-dump') !== null) {
		event.preventDefault();
		const request: ClickMessage = { type: 'heapDump' };
		withSession(request, target);
		api.postMessage(request);
		return;
	}
	const source = closestData(target, 'data-source-file');
	if (source !== null) {
		event.preventDefault();
		const message: ClickMessage = {
			type: 'openSource',
			file: source.dataset.sourceFile ?? '',
			name: source.dataset.sourceName ?? source.dataset.sourceFile ?? '',
			...(source.dataset.fallbackAddress !== undefined ? { fallbackAddress: source.dataset.fallbackAddress } : {}),
			line: Number(source.dataset.sourceLine)
		};
		withSession(message, target);
		api.postMessage(message);
		return;
	}
	const address = closestAddress(target);
	if (address === null) {
		return;
	}
	event.preventDefault();
	const message: ClickMessage = { type: 'openMemory', address };
	withSession(message, target);
	api.postMessage(message);
});

function withSession(message: ClickMessage, target: HTMLElement | null): void {
	const sessionId = closestSession(target);
	if (sessionId !== null) {
		message.sessionId = sessionId;
	}
}

function closestAddress(element: HTMLElement | null): string | null {
	const anchor = closestData(element, 'data-address');
	return anchor === null ? null : anchor.dataset.address ?? null;
}

function closestSession(element: HTMLElement | null): string | null {
	const node = closestData(element, 'data-session');
	return node === null ? null : node.dataset.session ?? null;
}

function closestData(element: HTMLElement | null, attribute: string): HTMLElement | null {
	let current: HTMLElement | null = element;
	while (current !== null) {
		if (current.hasAttribute(attribute)) {
			return current;
		}
		current = current.parentElement;
	}
	return null;
}

/** The body is the scroller; table cards scroll sideways on their own. */
function bodyOf(container: HTMLElement): HTMLElement | null {
	return container.querySelector<HTMLElement>('.gdr-body');
}

function tablesOf(container: HTMLElement): HTMLElement[] {
	// Reason: Array.from rather than spread: the DOM iterable lib is not loaded.
	return Array.from(container.querySelectorAll<HTMLElement>('.gdr-scroll, .gdr-heap-map'));
}

function scrollKey(table: HTMLElement): string {
	const kind = table.closest<HTMLElement>('section')?.dataset.kind ?? 'system';
	return `${kind}:${table.querySelector('table')?.className ?? 'map'}`;
}

/** Record where the reader is before the markup is replaced. */
function captureScroll(container: HTMLElement): ScrollMemory {
	const body = bodyOf(container);
	return {
		session: body?.dataset.session ?? null,
		top: body?.scrollTop ?? 0,
		tables: Object.fromEntries(tablesOf(container).map((table) => [scrollKey(table), table.scrollLeft]))
	};
}

/**
 * Put the reader back where they were, if the same session is still shown.
 *
 * A refresh rebuilds every node, so without this each stop scrolls the panel
 * to the top. A different session (a tab switch) starts at the top on purpose.
 */
function restoreScroll(container: HTMLElement, memory: ScrollMemory): void {
	const body = bodyOf(container);
	if (body !== null && memory.session !== null && body.dataset.session === memory.session) {
		body.scrollTop = memory.top;
	}
	if (body?.dataset.session !== memory.session) { return; }
	tablesOf(container).forEach((table) => {
		const left = memory.tables[scrollKey(table)];
		if (left !== undefined) {
			table.scrollLeft = left;
		}
	});
}

/**
 * Replace a container with host-rendered panel markup.
 *
 * The string is trusted by contract: render.ts escapes every target-derived
 * string before this point (unit-tested against adversarial names), and the
 * message channel carries only extension-host output. This function is the
 * single seam where that contract is consumed, so the sanitizer boundary stays
 * auditable in one place.
 */
function applyTrustedHostMarkup(container: HTMLElement, html: string): void {
	hidePointerLabel();
	const memory = captureScroll(container);
	const disclosures = new Map(Array.from(container.querySelectorAll<HTMLDetailsElement>('details[data-disclosure]')).map((node) => [node.dataset.disclosure, node.open]));
	const fragment = document.createRange().createContextualFragment(html);
	container.replaceChildren(fragment);
	if (bodyOf(container)?.dataset.session === memory.session) {
		container.querySelectorAll<HTMLDetailsElement>('details[data-disclosure]').forEach((node) => {
			node.open = disclosures.get(node.dataset.disclosure) ?? node.open;
		});
	}
	wireLayout(container);
	wireHints(container);
	drawHeapCanvas(container);
	restoreScroll(container, memory);
	wireWaiters(container);
}

/** One selectable pointer label, outside table clipping; selection never navigates. */
let pointerLabel: HTMLDivElement | null = null;
let pointerTimer: ReturnType<typeof setTimeout> | undefined;
function hidePointerLabel(): void {
	clearTimeout(pointerTimer);
	if (pointerLabel !== null) { pointerLabel.hidden = true; }
}
function showPointerLabel(anchor: HTMLElement): void {
	const address = anchor.dataset.pointer;
	if (address === undefined || !/^0x[0-9a-f]+$/i.test(address)) { return; }
	clearTimeout(pointerTimer);
	if (pointerLabel === null) {
		pointerLabel = document.createElement('div');
		pointerLabel.className = 'gdr-pointer-label';
		pointerLabel.setAttribute('role', 'group');
		pointerLabel.setAttribute('aria-label', 'Raw pointer address');
		pointerLabel.addEventListener('mouseenter', () => clearTimeout(pointerTimer));
		pointerLabel.addEventListener('mouseleave', () => { pointerTimer = setTimeout(hidePointerLabel, 200); });
		document.body.appendChild(pointerLabel);
	}
	const raw = document.createElement('code');
	raw.textContent = address;
	pointerLabel.replaceChildren(raw);
	pointerLabel.hidden = false;
	const bounds = anchor.getBoundingClientRect();
	pointerLabel.style.left = `${Math.max(8, Math.min(bounds.left, window.innerWidth - pointerLabel.offsetWidth - 8))}px`;
	const below = bounds.bottom + pointerLabel.offsetHeight + 8 <= window.innerHeight;
	pointerLabel.style.top = `${Math.max(8, below ? bounds.bottom + 4 : bounds.top - pointerLabel.offsetHeight - 4)}px`;
	// Reason: placing a waiter label below its link covers the next waiter,
	// making that link impossible to click. Prefer beside the entire menu.
	const menu = anchor.closest<HTMLElement>('.gdr-waiter-menu')?.getBoundingClientRect();
	if (menu !== undefined) {
		const width = pointerLabel.offsetWidth;
		const height = pointerLabel.offsetHeight;
		if (menu.right + width + 8 <= window.innerWidth) {
			pointerLabel.style.left = `${menu.right + 4}px`;
		} else if (menu.left >= width + 8) {
			pointerLabel.style.left = `${menu.left - width - 4}px`;
		} else {
			pointerLabel.style.top = `${Math.max(8, menu.top >= height + 8 ? menu.top - height - 4 : menu.bottom + 4)}px`;
		}
	}
}
document.addEventListener('mouseover', (event) => {
	const anchor = (event.target as HTMLElement | null)?.closest<HTMLElement>('[data-pointer]');
	if (anchor !== null && anchor !== undefined) { showPointerLabel(anchor); }
});
document.addEventListener('mouseout', (event) => {
	if ((event.target as HTMLElement | null)?.closest('[data-pointer]')) {
		pointerTimer = setTimeout(hidePointerLabel, 200);
	}
});
document.addEventListener('focusin', (event) => {
	const anchor = (event.target as HTMLElement | null)?.closest<HTMLElement>('[data-pointer]');
	if (anchor !== null && anchor !== undefined) { showPointerLabel(anchor); }
});
document.addEventListener('focusout', (event) => {
	if ((event.relatedTarget as HTMLElement | null)?.closest('.gdr-pointer-label')) { return; }
	pointerTimer = setTimeout(hidePointerLabel, 200);
});
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') { hidePointerLabel(); } });
document.addEventListener('scroll', hidePointerLabel, true);
window.addEventListener('resize', hidePointerLabel);

/** Fixed overlays escape table clipping without changing row heights. */
function wireWaiters(container: HTMLElement): void {
	container.querySelectorAll<HTMLDetailsElement>('.gdr-waiter-fold').forEach((fold) => {
		const menu = fold.querySelector<HTMLElement>('.gdr-waiter-menu');
		const summary = fold.querySelector('summary');
		if (menu === null || summary === null) { return; }
		const place = (): void => {
			if (!fold.open) { return; }
			const bounds = summary.getBoundingClientRect();
			const width = Math.min(360, Math.max(160, window.innerWidth - 16));
			menu.style.width = `${width}px`;
			menu.style.left = `${Math.max(8, Math.min(bounds.left, window.innerWidth - width - 8))}px`;
			const below = window.innerHeight - bounds.bottom - 8;
			menu.style.maxHeight = `${Math.max(40, below >= 80 ? Math.min(240, below) : Math.min(240, bounds.top - 8))}px`;
			menu.style.top = below >= 80 ? `${bounds.bottom}px` : '';
			menu.style.bottom = below >= 80 ? '' : `${window.innerHeight - bounds.top}px`;
		};
		fold.addEventListener('toggle', place);
		bodyOf(container)?.addEventListener('scroll', place);
		fold.closest('.gdr-scroll')?.addEventListener('scroll', place);
		place();
	});
}

/** Keep only current advice, retain its order, and reveal genuinely new lines. */
function wireHints(container: HTMLElement): void {
	const session = bodyOf(container)?.dataset.session;
	if (session === undefined) { return; }
	const memory = hintMemory.get(session) ?? { texts: [], height: 88, top: 0, seen: false };
	hintMemory.set(session, memory);
	const box = container.querySelector<HTMLElement>('.gdr-hints');
	const scroll = box?.querySelector<HTMLElement>('.gdr-hints-scroll');
	const grip = box?.querySelector<HTMLElement>('.gdr-hints-grip');
	if (box === null || box === undefined || scroll === null || scroll === undefined || grip === null || grip === undefined) {
		memory.texts = []; memory.seen = true;
		return;
	}
	const items = Array.from(scroll.querySelectorAll<HTMLElement>('.gdr-hint'));
	const current = items.map((node) => node.textContent ?? '');
	const added = current.filter((text) => !memory.texts.includes(text));
	const order = [...memory.texts.filter((text) => current.includes(text)), ...added];
	for (const text of order) {
		const item = items.find((node) => node.textContent === text);
		if (item !== undefined) { scroll.append(item); }
	}
	const setHeight = (height: number): void => {
		memory.height = Math.min(480, Math.max(48, Math.round(height)));
		scroll.style.height = `${memory.height}px`;
		grip.setAttribute('aria-valuenow', String(memory.height));
	};
	setHeight(memory.height);
	if (added.length > 0) {
		scroll.scrollTop = scroll.scrollHeight;
		if (memory.seen) { box.classList.add('gdr-hints-new'); }
	} else { scroll.scrollTop = memory.top; }
	memory.top = scroll.scrollTop;
	memory.texts = order; memory.seen = true;
	scroll.addEventListener('scroll', () => { memory.top = scroll.scrollTop; });
	grip.addEventListener('keydown', (event: KeyboardEvent) => {
		if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') { return; }
		event.preventDefault();
		setHeight(memory.height + (event.key === 'ArrowDown' ? 24 : -24));
	});
	grip.addEventListener('pointerdown', (event: PointerEvent) => {
		if (event.button !== 0) { return; }
		event.preventDefault(); event.stopPropagation();
		const startY = event.clientY;
		const startHeight = memory.height;
		const pointerId = event.pointerId;
		grip.setPointerCapture?.(pointerId);
		const move = (next: PointerEvent): void => {
			if (next.pointerId === pointerId) { setHeight(startHeight + next.clientY - startY); }
		};
		const end = (next: PointerEvent): void => {
			if (next.pointerId !== pointerId) { return; }
			window.removeEventListener('pointermove', move);
			window.removeEventListener('pointerup', end);
			window.removeEventListener('pointercancel', end);
			if (grip.hasPointerCapture?.(pointerId)) { grip.releasePointerCapture(pointerId); }
		};
		window.addEventListener('pointermove', move);
		window.addEventListener('pointerup', end);
		window.addEventListener('pointercancel', end);
	});
}

// ---------------------------------------------------------------------------
// Layout gestures: table order, column widths, and the checkbox tree
// ---------------------------------------------------------------------------

// Reason: these rules restate `layoutEdits.ts` (the tested source). This file
// cannot import it: the webview loads it as a classic script with no bundler.
// Keep the bounds and the move rule in step with that module.
const MIN_WIDTH = 40;
const MAX_WIDTH = 1600;

function clampWidth(width: number): number {
	if (!Number.isFinite(width)) {
		return MIN_WIDTH;
	}
	return Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, Math.round(width)));
}

/**
 * Apply every saved column width through the CSSOM after a render.
 *
 * Widths arrive as `data-width` attributes because the CSP refuses `style=`;
 * `element.style` assignments are not markup, so the CSP does not block them.
 */
function applySavedWidths(container: HTMLElement): void {
	container.querySelectorAll<HTMLTableElement>('table').forEach((table) => {
		if (table.querySelector('th[data-width]') !== null) { applyColumnWidths(table); }
	});
}

/** Fixed col tracks keep automatic table layout from overriding a dragged width. */
function applyColumnWidths(table: HTMLTableElement): void {
	const headers = Array.from(table.querySelectorAll<HTMLElement>('thead th[data-col]'));
	// Reason: measure all natural widths before changing layout; otherwise the
	// first fixed column can distort the widths measured for its neighbours.
	const widths = headers.map((header) => clampWidth(Number(header.dataset.width) || header.getBoundingClientRect().width));
	const group = document.createElement('colgroup');
	headers.forEach((header, index) => {
		const width = widths[index]!;
		header.dataset.width = String(width);
		header.style.width = `${width}px`;
		const column = document.createElement('col');
		column.style.width = `${width}px`;
		group.append(column);
	});
	table.querySelector('colgroup')?.remove();
	table.prepend(group);
	table.classList.add('gdr-sized-table');
	table.style.tableLayout = 'fixed';
	table.style.width = `${widths.reduce((sum, width) => sum + width, 0)}px`;
}

/** The layout as the DOM now shows it: order, ticked boxes and resized columns. */
function layoutFromDom(container: HTMLElement): ClientLayout {
	const visibleOrder = Array.from(container.querySelectorAll<HTMLElement>('section.gdr-card[data-kind]')).map((card) => card.dataset.kind ?? '');
	const treeOrder = Array.from(container.querySelectorAll<HTMLElement>('[data-tree-kind]')).map((node) => node.dataset.treeKind ?? '');
	// Reason: retain hidden/unavailable tables in their slots while visible cards move.
	let next = 0;
	const order = treeOrder.map((kind) => visibleOrder.includes(kind) ? visibleOrder[next++] : kind);
	const hiddenTables: string[] = [];
	const tables: ClientLayout['tables'] = Object.fromEntries(Object.entries(savedLayout.tables).map(([kind, table]) => [kind, { ...table, widths: { ...table.widths }, hiddenColumns: [] }]));
	container.querySelectorAll<HTMLInputElement>('input[data-table]:not([data-column])').forEach((box) => {
		const kind = box.dataset.table ?? '';
		if (!box.checked && kind !== '') {
			hiddenTables.push(kind);
		}
	});
	container.querySelectorAll<HTMLInputElement>('input[data-table][data-column]').forEach((box) => {
		const kind = box.dataset.table ?? '';
		const column = box.dataset.column ?? '';
		if (kind === '' || column === '') {
			return;
		}
		tables[kind] = tables[kind] ?? { kind, widths: {}, hiddenColumns: [] };
		if (!box.checked) {
			tables[kind].hiddenColumns.push(column);
		}
	});
	container.querySelectorAll<HTMLElement>('th[data-width]').forEach((header) => {
		const card = header.closest<HTMLElement>('section.gdr-card[data-kind]');
		const kind = card?.dataset.kind ?? '';
		const column = header.dataset.col ?? '';
		const width = Number(header.style.width.replace('px', '')) || Number(header.dataset.width);
		if (kind === '' || column === '' || !Number.isFinite(width)) {
			return;
		}
		tables[kind] = tables[kind] ?? { kind, widths: {}, hiddenColumns: [] };
		tables[kind].widths[column] = clampWidth(width);
	});
	container.querySelectorAll<HTMLElement>('section[data-kind]').forEach((card) => {
		const kind = card.dataset.kind ?? '';
		tables[kind] = tables[kind] ?? { kind, widths: {}, hiddenColumns: [] };
		const visible = Array.from(card.querySelectorAll<HTMLElement>('th[data-col]')).map((header) => header.dataset.col ?? '');
		let previous: string[] = [];
		try {
			const saved: unknown = JSON.parse(card.dataset.columnOrder ?? '[]');
			if (Array.isArray(saved)) { previous = saved.filter((key): key is string => typeof key === 'string'); }
		} catch { /* A stale layout must not prevent new gestures. */ }
		tables[kind].columnOrder = [...new Set([...(previous.length > 0 ? previous : savedLayout.tables[kind]?.columnOrder ?? []), ...visible])];
	});
	return { order: [...new Set([...order, ...savedLayout.order])], hiddenTables, tables };
}

/** Post the reader's new layout to the host, which saves and redraws it. */
function postLayout(container: HTMLElement): void {
	savedLayout = layoutFromDom(container);
	syncCheckboxOrder(container);
	api.postMessage({ type: 'layout', layout: savedLayout });
}

function syncCheckboxOrder(container: HTMLElement): void {
	const tree = container.querySelector('.gdr-tree-root');
	const parents = Array.from(container.querySelectorAll<HTMLElement>('[data-tree-kind]'));
	for (const kind of savedLayout.order) {
		const parent = parents.find((node) => node.dataset.treeKind === kind);
		if (parent !== undefined) { tree?.appendChild(parent); }
	}
	for (const parent of parents) {
		const children = parent.querySelector('.gdr-tree-children');
		const nodes = Array.from(parent.querySelectorAll<HTMLElement>('.gdr-tree-child'));
		const keys = savedLayout.tables[parent.dataset.treeKind ?? '']?.columnOrder ?? [];
		const ordered = [...nodes].sort((a, b) => {
			if (parent.dataset.treeKind === 'heap' && a.classList.contains('gdr-tree-diag') !== b.classList.contains('gdr-tree-diag')) {
				return a.classList.contains('gdr-tree-diag') ? 1 : -1;
			}
			const rank = (node: HTMLElement): number => {
				const index = keys.indexOf(node.querySelector<HTMLInputElement>('input')?.dataset.column ?? '');
				return index < 0 ? keys.length : index;
			};
			return rank(a) - rank(b);
		});
		for (const node of ordered) { children?.appendChild(node); }
	}
}

function moveTable(container: HTMLElement, kind: string, targetKind: string, after: boolean): void {
	if (kind === targetKind) { return; }
	const parents = Array.from(container.querySelectorAll<HTMLElement>('[data-tree-kind]'));
	const source = parents.find((node) => node.dataset.treeKind === kind);
	const target = parents.find((node) => node.dataset.treeKind === targetKind);
	if (source === undefined || target === undefined || target.parentElement === null) { return; }
	target.parentElement.insertBefore(source, after ? target.nextSibling : target);
	const cards = Array.from(container.querySelectorAll<HTMLElement>('section[data-kind]'));
	// Reason: moving hidden tables still changes their saved slot, not visibility.
	for (const node of Array.from(target.parentElement.children)) {
		const card = cards.find((candidate) => candidate.dataset.kind === (node as HTMLElement).dataset.treeKind);
		if (card !== undefined) { card.parentElement?.appendChild(card); }
	}
	postLayout(container);
}

function canMoveColumn(source: HTMLElement, target: HTMLElement): boolean {
	const parent = source.closest<HTMLElement>('[data-tree-kind]');
	if (source === target || parent === null || parent !== target.closest('[data-tree-kind]')) { return false; }
	// Reason: heap properties and verdicts are separate tables; keep each group's
	// ordering within its real table instead of promising a cross-table move.
	return parent.dataset.treeKind !== 'heap' || source.classList.contains('gdr-tree-diag') === target.classList.contains('gdr-tree-diag');
}

function moveSelectionColumn(container: HTMLElement, source: HTMLElement, target: HTMLElement, after: boolean): void {
	target.parentElement?.insertBefore(source, after ? target.nextSibling : target);
	const parent = target.closest<HTMLElement>('[data-tree-kind]');
	const kind = parent?.dataset.treeKind ?? '';
	const order = Array.from(parent?.querySelectorAll<HTMLElement>('[data-tree-column]') ?? []).map((node) => node.dataset.treeColumn ?? '');
	const current = savedLayout.tables[kind] ?? { kind, widths: {}, hiddenColumns: [] };
	savedLayout.tables[kind] = { ...current, columnOrder: order };
	const card = Array.from(container.querySelectorAll<HTMLElement>('section[data-kind]')).find((node) => node.dataset.kind === kind);
	if (card !== undefined) {
		card.dataset.columnOrder = JSON.stringify(order);
		card.querySelectorAll('table').forEach((table) => {
			const headers = Array.from(table.querySelectorAll<HTMLElement>('thead th[data-col]'));
			const indexes = order.map((key) => headers.findIndex((header) => header.dataset.col === key)).filter((index) => index >= 0);
			table.querySelectorAll('tr').forEach((row) => {
				const cells = Array.from(row.children);
				for (const index of indexes) { row.appendChild(cells[index]); }
			});
			if (table.classList.contains('gdr-sized-table')) { applyColumnWidths(table); }
		});
	}
	postLayout(container);
}

/**
 * Wire the gestures once per render: table drag-reorder, the column grips,
 * and the checkbox tree. Each gesture ends with a `layout` message.
 */
function wireLayout(container: HTMLElement): void {
	applySavedWidths(container);
	let draggedKind: string | null = null;
	let draggedColumn: HTMLElement | null = null;
	let resizing = false;
	const clearDrag = (): void => {
		draggedKind = null;
		draggedColumn = null;
		container.querySelectorAll('.gdr-dragging, .gdr-drop-target').forEach((node) => node.classList.remove('gdr-dragging', 'gdr-drop-target'));
	};
	const startTable = (event: DragEvent, kind: string, source: HTMLElement): void => {
		event.stopPropagation();
		draggedKind = kind;
		source.classList.add('gdr-dragging');
		if (event.dataTransfer) { event.dataTransfer.effectAllowed = 'move'; }
		event.dataTransfer?.setData('text/plain', kind);
	};
	const wireTableDrop = (target: HTMLElement, kind: string): void => {
		target.addEventListener('dragover', (event: DragEvent) => {
			if (draggedKind === null) { return; }
			event.preventDefault(); event.stopPropagation();
			target.classList.add('gdr-drop-target');
		});
		target.addEventListener('dragleave', () => target.classList.remove('gdr-drop-target'));
		target.addEventListener('drop', (event: DragEvent) => {
			if (draggedKind === null) { return; }
			event.preventDefault(); event.stopPropagation();
			const rect = target.getBoundingClientRect();
			moveTable(container, draggedKind, kind, event.clientY > rect.top + rect.height / 2);
			clearDrag();
		});
	};

	container.querySelectorAll<HTMLElement>('section.gdr-card[data-kind]').forEach((card) => {
		const title = card.querySelector<HTMLElement>('.gdr-card-title[draggable]');
		title?.addEventListener('dragstart', (event: DragEvent) => startTable(event, card.dataset.kind ?? '', title));
		title?.addEventListener('dragend', clearDrag);
		wireTableDrop(card, card.dataset.kind ?? '');
	});
	container.querySelectorAll<HTMLElement>('[data-tree-kind]').forEach((parent) => {
		const summary = parent.querySelector<HTMLElement>('summary');
		summary?.addEventListener('dragstart', (event: DragEvent) => startTable(event, parent.dataset.treeKind ?? '', summary));
		summary?.addEventListener('dragend', clearDrag);
		wireTableDrop(parent, parent.dataset.treeKind ?? '');
	});
	container.querySelectorAll<HTMLElement>('[data-tree-column]').forEach((column) => {
		column.addEventListener('dragstart', (event: DragEvent) => {
			event.stopPropagation();
			draggedColumn = column;
			column.classList.add('gdr-dragging');
			if (event.dataTransfer) { event.dataTransfer.effectAllowed = 'move'; }
			event.dataTransfer?.setData('text/plain', column.dataset.treeColumn ?? '');
		});
		column.addEventListener('dragend', clearDrag);
		column.addEventListener('dragover', (event: DragEvent) => {
			if (draggedColumn === null || !canMoveColumn(draggedColumn, column)) { return; }
			event.preventDefault(); event.stopPropagation(); column.classList.add('gdr-drop-target');
		});
		column.addEventListener('dragleave', () => column.classList.remove('gdr-drop-target'));
		column.addEventListener('drop', (event: DragEvent) => {
			if (draggedColumn === null || !canMoveColumn(draggedColumn, column)) { return; }
			event.preventDefault(); event.stopPropagation();
			const rect = column.getBoundingClientRect();
			moveSelectionColumn(container, draggedColumn, column, event.clientX > rect.left + rect.width / 2);
			clearDrag();
		});
	});

	container.querySelectorAll<HTMLElement>('th[data-col][draggable]').forEach((header) => {
		header.addEventListener('dragstart', (event: DragEvent) => {
			event.stopPropagation();
			if (resizing || (event.target as HTMLElement).closest('.gdr-col-grip') !== null) {
				event.preventDefault();
				return;
			}
			header.classList.add('gdr-column-dragging');
			event.dataTransfer?.setData('text/plain', header.dataset.col ?? '');
		});
		header.addEventListener('dragend', () => header.classList.remove('gdr-column-dragging'));
		header.addEventListener('dragover', (event: DragEvent) => {
			if (draggedKind !== null || draggedColumn !== null) { return; }
			event.preventDefault(); event.stopPropagation();
		});
		header.addEventListener('drop', (event: DragEvent) => {
			if (draggedKind !== null || draggedColumn !== null) { return; }
			event.preventDefault(); event.stopPropagation();
			const table = header.closest('table');
			const dragged = table?.querySelector<HTMLElement>('th.gdr-column-dragging');
			if (table === null || dragged === null || dragged === undefined || dragged === header) { return; }
			const from = Array.from(header.parentElement!.children).indexOf(dragged);
			const to = Array.from(header.parentElement!.children).indexOf(header);
			const after = event.clientX > header.getBoundingClientRect().left + header.getBoundingClientRect().width / 2;
			table.querySelectorAll('tr').forEach((row) => {
				const cell = row.children[from];
				const target = row.children[to];
				if (cell !== undefined && target !== undefined) { row.insertBefore(cell, after ? target.nextSibling : target); }
			});
			const card = header.closest<HTMLElement>('section[data-kind]');
			if (card !== null) {
				const keys = [...new Set([...(savedLayout.tables[card.dataset.kind ?? '']?.columnOrder ?? []), ...Array.from(card.querySelectorAll<HTMLElement>('th[data-col]')).map((node) => node.dataset.col ?? '')])];
				const rest = keys.filter((key) => key !== dragged.dataset.col);
				const insert = rest.indexOf(header.dataset.col ?? '');
				rest.splice(insert + (after ? 1 : 0), 0, dragged.dataset.col ?? '');
				card.dataset.columnOrder = JSON.stringify(rest);
			}
			if (table.classList.contains('gdr-sized-table')) { applyColumnWidths(table); }
			postLayout(container);
		});
	});

	container.querySelectorAll<HTMLElement>('.gdr-col-grip').forEach((grip) => {
		// Reason: prevent the draggable header's native mouse drag as well as
		// the pointer gesture; resizing and column reordering are independent.
		grip.addEventListener('mousedown', (event) => { event.preventDefault(); event.stopPropagation(); });
		grip.addEventListener('dragstart', (event) => { event.preventDefault(); event.stopPropagation(); });
		grip.addEventListener('pointerdown', (event: PointerEvent) => {
			const header = grip.closest<HTMLElement>('th');
			const table = header?.closest('table');
			if (header === null || header === undefined || table === null || table === undefined || event.button !== 0 || resizing) { return; }
			event.preventDefault(); event.stopPropagation();
			resizing = true;
			const draggable = header.draggable;
			header.draggable = false;
			applyColumnWidths(table);
			const startX = event.clientX;
			const startWidth = Number(header.dataset.width);
			const pointerId = event.pointerId;
			grip.setPointerCapture?.(pointerId);
			const move = (moveEvent: PointerEvent): void => {
				if (moveEvent.pointerId !== pointerId) { return; }
				header.dataset.width = String(clampWidth(startWidth + moveEvent.clientX - startX));
				applyColumnWidths(table);
			};
			const up = (upEvent: PointerEvent): void => {
				if (upEvent.pointerId !== pointerId) { return; }
				if (upEvent.type === 'pointerup') { move(upEvent); }
				window.removeEventListener('pointermove', move);
				window.removeEventListener('pointerup', up);
				window.removeEventListener('pointercancel', up);
				if (grip.hasPointerCapture?.(pointerId)) { grip.releasePointerCapture(pointerId); }
				resizing = false;
				header.draggable = draggable;
				postLayout(container);
			};
			window.addEventListener('pointermove', move);
			window.addEventListener('pointerup', up);
			window.addEventListener('pointercancel', up);
		});
	});

	container.querySelectorAll<HTMLInputElement>('input[data-table]').forEach((box) => {
		box.addEventListener('click', (event) => event.stopPropagation());
		box.addEventListener('change', () => {
			postLayout(container);
		});
	});
}

// ---------------------------------------------------------------------------
// Heap canvas: paint the block layout, and read the block under the pointer
// ---------------------------------------------------------------------------

// Reason: the serialized grid is the single source for painting and hover.
const HEAP_BACKGROUND: [number, number, number] = [0x1e, 0x1e, 0x1e];
const HEAP_FALLBACK_FREE = '#c8c8c8';
const HEAP_FALLBACK_UNKNOWN = '#4f8fd6';

/** One block as the heap-dump envelope lists it: address, size, used, owner. */
type ClientBlock = [number, number, boolean, string | null];

/** The heap map the host sent, narrowed at the boundary by `isHeapMap`. */
interface ClientHeapMap {
	grid?: { width: number; height: number; scale: number; runs: [number, number, number][] };
	base: number;
	end: number;
	owners: boolean;
	blocks: ClientBlock[];
	palette: { owners: Record<string, string>; free: string; unknown: string };
}

function isHeapMap(value: unknown): value is ClientHeapMap {
	if (typeof value !== 'object' || value === null) {
		return false;
	}
	const map = value as Record<string, unknown>;
	if (typeof map.base !== 'number' || typeof map.end !== 'number' || !Array.isArray(map.blocks)) {
		return false;
	}
	const palette = map.palette as Record<string, unknown> | undefined;
	if (typeof palette !== 'object' || palette === null || typeof palette.free !== 'string' || typeof palette.unknown !== 'string') {
		return false;
	}
	return map.blocks.every(
		(block: unknown) =>
			Array.isArray(block) &&
			block.length === 4 &&
			typeof block[0] === 'number' &&
			typeof block[1] === 'number' &&
			typeof block[2] === 'boolean' &&
			(block[3] === null || typeof block[3] === 'string')
	);
}

/** A `#rrggbb` colour as its three channels; anything else is the fallback grey. */
function rgbOf(hex: string, fallback: string): [number, number, number] {
	const pattern = /^#[0-9a-fA-F]{6}$/;
	const value = pattern.test(hex) ? hex : fallback;
	const number = parseInt(value.slice(1), 16);
	return [(number >> 16) & 0xff, (number >> 8) & 0xff, number & 0xff];
}

/** The colour a block paints with: free grey, its owner's colour, or the unknown colour. */
function colourOf(block: ClientBlock, map: ClientHeapMap): [number, number, number] {
	const [, , used, owner] = block;
	if (!used) {
		return rgbOf(map.palette.free, HEAP_FALLBACK_FREE);
	}
	if (owner !== null && Object.prototype.hasOwnProperty.call(map.palette.owners, owner)) {
		return rgbOf(map.palette.owners[owner], HEAP_FALLBACK_UNKNOWN);
	}
	return rgbOf(map.palette.unknown, HEAP_FALLBACK_UNKNOWN);
}

/** The readout text for a block: the same wording as heapCanvas.ts `readoutFor`. */
function heapReadout(block: ClientBlock, owners: boolean): string {
	const [, size, used, owner] = block;
	if (!used) {
		return `owner:(free), size: ${size} B`;
	}
	const name = owners && owner !== null ? owner : 'N/A';
	return `owner:${name}, size: ${size} B`;
}

/**
 * Paint the stored heap map onto the rendered canvas, at least six pixels per cell,
 * and wire the hover. The painted cell index is kept, so the hover names exactly
 * the block the pixel shows, including where blocks overlap.
 */
function drawHeapCanvas(container: HTMLElement): void {
	const canvas = container.querySelector<HTMLCanvasElement>('canvas[data-heap-canvas]');
	const map = heapMapData;
	if (canvas === null || map === null) {
		return;
	}
	const context = canvas.getContext('2d');
	if (context === null) {
		return;
	}
	const grid = map.grid;
	if (grid === undefined || grid.width <= 0 || grid.height <= 0) { return; }
	const columns = grid.width;
	const rows = grid.height;
	const scale = Math.max(6, grid.scale);
	const cells = columns * rows;
	const owner = new Int32Array(cells).fill(-1);

	// Reason: consume the exact Python allocation, not a second rasterizer whose
	// rounding can overwrite tiny blocks and disagree with PNG/hover.
	for (const [start, stop, index] of grid.runs) { owner.fill(index, start, stop); }
	canvas.width = columns * scale;
	canvas.height = rows * scale;
	canvas.style.width = `${canvas.width}px`;
	canvas.style.height = `${canvas.height}px`;
	const image = context.createImageData(canvas.width, canvas.height);
	for (let cell = 0; cell < cells; cell += 1) {
		const index = owner[cell];
		const [red, green, blue] = index < 0 ? HEAP_BACKGROUND : colourOf(map.blocks[index], map);
		for (let y = 0; y < scale; y += 1) {
			for (let x = 0; x < scale; x += 1) {
				const pixel = ((Math.floor(cell / columns) * scale + y) * canvas.width + (cell % columns) * scale + x) * 4;
				image.data[pixel] = red; image.data[pixel + 1] = green; image.data[pixel + 2] = blue; image.data[pixel + 3] = 255;
			}
		}
	}
	context.putImageData(image, 0, 0);

	const tooltip = container.querySelector<HTMLElement>('[data-heap-tooltip]');
	// Reason: assigning the handler replaces the previous one, so a redraw never
	// stacks listeners on the same canvas.
	canvas.onmousemove = (event: MouseEvent): void => {
		const rect = canvas.getBoundingClientRect();
		if (rect.width <= 0 || rect.height <= 0) { return; }
		const column = Math.floor(((event.clientX - rect.left) / rect.width) * columns);
		const row = Math.floor(((event.clientY - rect.top) / rect.height) * rows);
		if (column < 0 || column >= columns || row < 0 || row >= rows) {
			if (tooltip !== null) { tooltip.hidden = true; }
			return;
		}
		const index = owner[row * columns + column];
		// Reason: textContent, never innerHTML: an owner name is target data.
		const text = index < 0 ? 'unknown/unreadable arena span' : heapReadout(map.blocks[index], map.owners);
		// Reason: the custom floating tooltip is the single hover surface. A
		// native `title` here would make Chromium show a second label beside it.
		canvas.removeAttribute('title');
		if (tooltip !== null) {
			tooltip.textContent = text;
			tooltip.hidden = false;
			tooltip.style.left = `${Math.max(4, Math.min(event.clientX + 12, window.innerWidth - tooltip.offsetWidth - 8))}px`;
			tooltip.style.top = `${Math.max(4, Math.min(event.clientY + 16, window.innerHeight - tooltip.offsetHeight - 8))}px`;
		}
	};
	canvas.onmouseleave = (): void => {
		if (tooltip !== null) { tooltip.hidden = true; }
	};
	canvas.onpointermove = canvas.onmousemove;
	canvas.onpointerleave = canvas.onmouseleave;
	const hideTooltip = (): void => { if (tooltip !== null) { tooltip.hidden = true; } };
	const fold = canvas.closest<HTMLDetailsElement>('.gdr-heap-fold');
	if (fold !== null) { fold.ontoggle = hideTooltip; }
	const scroller = canvas.closest<HTMLElement>('.gdr-heap-map');
	if (scroller !== null) { scroller.onscroll = hideTooltip; }
	const body = bodyOf(container);
	if (body !== null) { body.onscroll = hideTooltip; }
}
