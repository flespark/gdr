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
	type: 'openMemory' | 'openSource' | 'heapDump' | 'layout' | 'refresh' | 'selectSession' | 'resizeHeap' | 'resetHeapSize';
	sessionId?: string;
	columns?: number;
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
	if (payload.type === 'heapGrid') {
		const sessionId = (message as { sessionId?: string }).sessionId;
		if (sessionId !== bodyOf(root)?.dataset.session) { return; }
		const sized = narrowSizedGrid((payload as { grid?: unknown }).grid);
		if (sized === null) { return; }
		// Reason: the host replays the producer layout for the drag width;
		// the envelope grid stays untouched for reset and the PNG.
		heapViewGrid = sized;
		const rowBytes = (payload as { rowBytes?: unknown }).rowBytes;
		heapViewRowBytes = typeof rowBytes === 'number' && Number.isFinite(rowBytes) && rowBytes >= 0 ? Math.round(rowBytes) : 0;
		drawHeapCanvas(root);
		if (heapResize !== null) {
			placeResizeLabel(root, heapResize.x, heapResize.y);
		}
	}
});

document.addEventListener('click', (event: MouseEvent) => {
	const target = event.target as HTMLElement | null;
	const tab = target?.closest<HTMLElement>('.gdr-tab');
	if (tab !== null && tab !== undefined) {
		api.postMessage({ type: 'selectSession', sessionId: tab.dataset.session });
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

/** Disclosure open-state per session: switching tabs must not reset choices. */
const disclosureMemory = new Map<string, boolean>();

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
	const session = bodyOf(container)?.dataset.session ?? '';
	// Reason: the outgoing session's choices are saved first, so switching
	// tabs keeps every disclosure (including the heap map) as the reader
	// left it instead of springing back to the markup default.
	container.querySelectorAll<HTMLDetailsElement>('details[data-disclosure]').forEach((node) => {
		disclosureMemory.set(`${session}:${node.dataset.disclosure ?? ''}`, node.open);
	});
	const fragment = document.createRange().createContextualFragment(html);
	container.replaceChildren(fragment);
	const next = bodyOf(container)?.dataset.session ?? '';
	container.querySelectorAll<HTMLDetailsElement>('details[data-disclosure]').forEach((node) => {
		const key = `${next}:${node.dataset.disclosure ?? ''}`;
		if (disclosureMemory.has(key)) {
			node.open = disclosureMemory.get(key) ?? node.open;
		}
	});
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

// Reason: the grip drag posts column counts while the host replays the
// producer layout; local listeners stay on window so a redraw mid-drag
// cannot strand them on a replaced grip element.
window.addEventListener('pointermove', (event: PointerEvent) => {
	if (heapResize === null) { return; }
	heapResize.x = event.clientX;
	heapResize.y = event.clientY;
	// Reason: only the minimum is clamped; the drag sets whatever width the
	// reader pulls, and the viewport scrolls past the old budget.
	const columns = Math.max(heapResize.minCols, heapResize.startColumns + Math.round((event.clientX - heapResize.startX) / heapResize.cellPx));
	const request: ClickMessage = { type: 'resizeHeap', columns };
	withSession(request, heapResize.container.querySelector<HTMLElement>('[data-heap-grip]'));
	api.postMessage(request);
	placeResizeLabel(heapResize.container, event.clientX, event.clientY);
});

window.addEventListener('pointerup', () => {
	if (heapResize === null) { return; }
	const { container } = heapResize;
	heapResize = null;
	hideHeapResize(container);
});

// Reason: the corner zoom is a pure display transform, so tracking lives on
// window beside the column drag: a redraw mid-drag cannot strand it.
window.addEventListener('pointermove', (event: PointerEvent) => {
	if (heapCornerDrag === null) { return; }
	const { container, originX, originY, baseW, baseH } = heapCornerDrag;
	const canvas = container.querySelector<HTMLCanvasElement>('canvas[data-heap-canvas]');
	if (canvas === null) { return; }
	const want = Math.max((event.clientX - originX) / baseW, (event.clientY - originY) / baseH);
	if (!Number.isFinite(want) || want <= 0) { return; }
	heapViewZoom = Math.min(4, Math.max(0.5, want));
	const grid = heapViewGrid ?? heapMapData?.grid;
	if (grid === undefined) { return; }
	sizeHeapCanvas(canvas, grid.width, heapUnits(grid.height, grid.folds ?? []).length, Math.max(6, grid.scale));
});

window.addEventListener('pointerup', () => {
	heapCornerDrag = null;
});
window.addEventListener('pointercancel', () => {
	heapCornerDrag = null;
});
window.addEventListener('pointercancel', () => {
	if (heapResize === null) { return; }
	const { container } = heapResize;
	heapResize = null;
	hideHeapResize(container);
});

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
// Reason: fold hatches are 2-pixel 45-degree stripes, crisp at any cell size
// where a soft shadow blurred into mud on low-density exports.
const HEAP_FOLD_HATCH: [number, number, number] = [0x90, 0x90, 0x90];
// Reason: notes carry no block colour; a mid grey stays visible on both the
// dark canvas and a light panel theme, unlike the PNG's dark swatch.
const HEAP_NOTE_SWATCH = '#808080';
// Reason: short rankings stay in one column like the PNG below eight rows;
// wrapping only pays off past that, where two columns stay narrower.
const HEAP_LEGEND_WRAP_MIN = 8;

/** One block as the heap-dump envelope lists it: address, size, used, owner. */
type ClientBlock = [number, number, boolean, string | null];

/** One collapsed unused range: hatched bands drawn before grid `row`. */
interface ClientFold {
	row: number;
	rows?: number;
	height: number;
	bytes: number;
	kind: string;
	start: number;
	end: number;
}

/**
 * The host-computed relayout: shape-checked, not parsed. The host is the
 * trusted sender (origin-checked channel) replaying our own planner output
 * over structured clone, unlike target-influenced envelope bytes.
 */
function narrowSizedGrid(value: unknown): ClientGrid | null {
	if (typeof value !== 'object' || value === null) { return null; }
	const grid = value as Record<string, unknown>;
	if (typeof grid.width !== 'number' || typeof grid.height !== 'number' ||
		!Number.isInteger(grid.width) || !Number.isInteger(grid.height) ||
		grid.width <= 0 || grid.height <= 0 || typeof grid.scale !== 'number' || !Array.isArray(grid.runs)) {
		return null;
	}
	// SAFETY: runs/folds element shapes are host-built by our own planner from
	// validated envelope data over an origin-checked channel; malsized cells
	// only mispaint, never escape the canvas.
	return grid as unknown as ClientGrid;
}

/** The heap block layout: the envelope grid or a host-computed relayout. */
interface ClientGrid {
	width: number;
	height: number;
	scale: number;
	runs: [number, number, number][];
	folds?: ClientFold[];
}

/** Resize drag state; module-level so redraws never strand it. */
let heapResize: {
	container: HTMLElement;
	startX: number;
	startColumns: number;
	x: number;
	y: number;
	cellPx: number;
	minCols: number;
} | null = null;

/** Relayout override from the host; null follows the envelope grid. */
let heapViewGrid: ClientGrid | null = null;
/** Average kept bytes per row of the override, shown live while dragging. */
let heapViewRowBytes = 0;
/** Display zoom factor; module-level so redraws never reset the reader's choice. */
let heapViewZoom = 1;
/** Corner drag state; module-level so redraws never strand it. */
let heapCornerDrag: {
	container: HTMLElement;
	originX: number;
	originY: number;
	baseW: number;
	baseH: number;
} | null = null;

/** The heap map the host sent, narrowed at the boundary by `isHeapMap`. */
interface ClientHeapMap {
	grid?: ClientGrid;
	base: number;
	end: number;
	owners: boolean;
	truncated?: boolean;
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

/** An address as the endpoint labels and the hover print it: full-precision hex. */
function heapAddress(addr: number): string {
	return `0x${addr.toString(16)}`;
}

/** The readout text for a block: the same wording as heapCanvas.ts `readoutFor`. */
function heapReadout(block: ClientBlock, owners: boolean): string {
	const [address, size, used, owner] = block;
	const range = `start: ${heapAddress(address)}, end: ${heapAddress(address + size)}`;
	if (!used) {
		return `owner:(free), size: ${size} B\n${range}`;
	}
	const name = owners && owner !== null ? owner : 'N/A';
	return `owner:${name}, size: ${size} B\n${range}`;
}

/** One legend row beside the bitmap: the swatch colour and the occupancy text. */
interface HeapLegendRow {
	colour: string;
	text: string;
}

/** Bytes selected by `take`, the shared total behind each legend row. */
function heapSumSizes(blocks: ClientBlock[], take: (block: ClientBlock) => boolean): number {
	return blocks.reduce((sum, block) => sum + (take(block) ? block[1] : 0), 0);
}

/** The address span selected blocks cover, mirroring the PNG legend rows. */
function heapSpanOf(blocks: ClientBlock[], take: (block: ClientBlock) => boolean): [number, number] {
	let start = Number.POSITIVE_INFINITY;
	let end = 0;
	for (const block of blocks) {
		if (!take(block)) { continue; }
		start = Math.min(start, block[0]);
		end = Math.max(end, block[0] + block[1]);
	}
	return [start, end];
}

/** One painted unit: a grid row of cells, or one row of a divider band. */
type HeapUnit = { type: 'row'; row: number } | { type: 'fold'; fold: ClientFold };

/**
 * Paint order in content units: each divider band, height rows tall, before
 * its grid row. Mirrors heapCanvas.ts `unitRows`; this file stays import-free.
 */
function heapUnits(rows: number, folds: ClientFold[]): HeapUnit[] {
	const byFirst = new Map<number, ClientFold[]>();
	for (const fold of folds) {
		const list = byFirst.get(fold.row) ?? [];
		list.push(fold);
		byFirst.set(fold.row, list);
	}
	const units: HeapUnit[] = [];
	let row = 0;
	while (row < rows) {
		const bands = byFirst.get(row) ?? [];
		if (bands.length > 0) {
			for (const fold of bands) {
				for (let span = 0; span < fold.height; span += 1) { units.push({ type: 'fold', fold }); }
			}
			// Reason: older envelopes carry no row count; one folded row each.
			const covered = Math.max(...bands.map((fold) => fold.rows ?? 1));
			row += covered;
		} else {
			units.push({ type: 'row', row });
			row += 1;
		}
	}
	return units;
}

/** The readout text for a divider: the same wording as heapCanvas.ts `foldReadout`. */
function heapFoldReadout(fold: ClientFold): string {
	return `folded: ${fold.bytes} B (${fold.kind})\nstart: ${heapAddress(fold.start)}, end: ${heapAddress(fold.end)}`;
}

/** What the pinned label shows: a block, a divider, or an unreadable gap. */
type HeapLabelHit =
	| { kind: 'block'; block: ClientBlock; owners: boolean }
	| { kind: 'fold'; fold: ClientFold }
	| { kind: 'gap' };

/**
 * Fill the pinned label: the owner line as text, the address span as
 * memory-view links. DOM-built, never innerHTML: owner names are target
 * data, and the document click handler routes `data-address` to openMemory.
 * A literal newline keeps textContent identical to the readout strings.
 */
function paintHeapLabel(tooltip: HTMLElement, hit: HeapLabelHit): void {
	tooltip.replaceChildren();
	if (hit.kind === 'gap') {
		tooltip.textContent = 'unknown/unreadable arena span';
		return;
	}
	const start = hit.kind === 'fold' ? hit.fold.start : hit.block[0];
	const end = hit.kind === 'fold' ? hit.fold.end : hit.block[0] + hit.block[1];
	const head = hit.kind === 'fold' ? heapFoldReadout(hit.fold) : heapReadout(hit.block, hit.owners);
	const firstLine = head.split('\n')[0] ?? head;
	const row = document.createElement('div');
	row.className = 'gdr-heap-addrs';
	row.append('start: ', heapAddrLink(start), ', end: ', heapAddrLink(end));
	tooltip.append(firstLine, '\n', row);
}

/** One address link: the document click handler opens it in the memory view. */
function heapAddrLink(addr: number): HTMLButtonElement {
	const link = document.createElement('button');
	link.type = 'button';
	link.className = 'gdr-heap-addr-link';
	link.dataset.address = heapAddress(addr);
	link.textContent = heapAddress(addr);
	return link;
}

/**
 * The occupancy ranking beside the bitmap, in the PNG legend's own order.
 * Mirrors heapCanvas.ts `legendEntries`; this file stays import-free.
 */
function heapLegend(map: ClientHeapMap): HeapLegendRow[] {
	const owned = new Map<string, ClientBlock[]>();
	for (const block of map.blocks) {
		const [, , used, owner] = block;
		if (used && owner !== null) {
			const list = owned.get(owner) ?? [];
			list.push(block);
			owned.set(owner, list);
		}
	}
	const rows: HeapLegendRow[] = [];
	for (const [name, list] of owned) {
		const [start, end] = heapSpanOf(list, () => true);
		rows.push({ colour: map.palette.owners[name] ?? map.palette.unknown, text: `${name}: ${heapSumSizes(list, () => true)}B, ${heapAddress(start)}-${heapAddress(end)}` });
	}
	const unowned = map.blocks.filter((block) => block[2] && block[3] === null);
	if (unowned.length > 0) {
		const [start, end] = heapSpanOf(unowned, () => true);
		rows.push({ colour: map.palette.unknown, text: `N/A: ${heapSumSizes(unowned, () => true)}B, ${heapAddress(start)}-${heapAddress(end)}` });
	}
	const free = map.blocks.filter((block) => !block[2]);
	if (free.length > 0) {
		const [start, end] = heapSpanOf(free, () => true);
		rows.push({ colour: map.palette.free, text: `(free): ${heapSumSizes(free, () => true)}B, ${heapAddress(start)}-${heapAddress(end)}` });
	}
	const grid = map.grid;
	const hasUnknown = grid !== undefined &&
		(grid.runs.some((run) => run[2] < 0) || (grid.folds ?? []).some((fold) => fold.kind !== 'free'));
	if (hasUnknown) {
		rows.push({ colour: HEAP_NOTE_SWATCH, text: 'unknown/unreadable arena span' });
	}
	if (map.truncated === true) {
		rows.push({ colour: HEAP_NOTE_SWATCH, text: 'incomplete block walk' });
	}
	return rows;
}

/**
 * Paint the stored heap map onto the rendered canvas, at least six pixels per cell,
 * and wire the hover. The painted cell index is kept, so the hover names exactly
 * the block the pixel shows, including where blocks overlap.
 */
function drawHeapCanvas(container: HTMLElement): void {
	const canvas = container.querySelector<HTMLCanvasElement>('canvas[data-heap-canvas]');
	const viewport = container.querySelector<HTMLElement>('.gdr-heap-map');
	const grip = container.querySelector<HTMLElement>('[data-heap-grip]');
	const legend = container.querySelector<HTMLElement>('[data-heap-legend]');
	const addrStart = container.querySelector<HTMLElement>('[data-heap-addr-start]');
	const addrEnd = container.querySelector<HTMLElement>('[data-heap-addr-end]');
	const tooltip = container.querySelector<HTMLElement>('[data-heap-tooltip]');
	const map = heapMapData;
	// Reason: assigned handlers outlive an early return (the empty section
	// wires its toggle before any canvas exists), so shared state is
	// declared up front and never sits in the temporal dead zone.
	let pan: { x: number; y: number; left: number; top: number; moved: boolean } | null = null;
	let pinnedKey: string | null = null;
	const hideTooltip = (): void => { pinnedKey = null; if (tooltip !== null) { tooltip.hidden = true; } };
	const fold = container.querySelector<HTMLDetailsElement>('.gdr-heap-fold');
	if (fold !== null) {
		// Reason: expanding the empty section requests the bitmap, so the
		// summary itself is the draw control; assigning replaces any older
		// handler, so a redraw never stacks listeners on the same fold.
		fold.ontoggle = (): void => {
			hideTooltip();
			if (fold.open && fold.querySelector('canvas[data-heap-canvas]') === null) {
				const request: ClickMessage = { type: 'heapDump' };
				withSession(request, fold);
				api.postMessage(request);
			}
		};
	}
	if (canvas === null || map === null) {
		return;
	}
	const context = canvas.getContext('2d');
	if (context === null) {
		return;
	}
	const grid = heapViewGrid ?? map.grid;
	if (grid === undefined || grid.width <= 0 || grid.height <= 0) {
		// Reason: an empty grid must not leave the previous map's shells behind.
		legend?.replaceChildren();
		if (addrStart !== null) { addrStart.textContent = ''; }
		if (addrEnd !== null) { addrEnd.textContent = ''; }
		return;
	}
	const columns = grid.width;
	const rows = grid.height;
	const folds = [...(grid.folds ?? [])].sort((a, b) => a.row - b.row);
	const scale = Math.max(6, grid.scale);
	// Reason: the hatch period tracks the cell size, so a coarser envelope
	// scale keeps the same 2-on-2-off diagonal rhythm as the PNG.
	const period = Math.max(2, Math.round(scale * 2 / 3));
	const half = period / 2;
	const units = heapUnits(rows, folds);
	const cells = columns * rows;
	const owner = new Int32Array(cells).fill(-1);

	// Reason: consume the exact Python allocation, not a second rasterizer whose
	// rounding can overwrite tiny blocks and disagree with PNG/hover.
	for (const [start, stop, index] of grid.runs) { owner.fill(index, start, stop); }
	canvas.width = columns * scale;
	canvas.height = units.length * scale;
	sizeHeapCanvas(canvas, columns, units.length, scale);
	const image = context.createImageData(canvas.width, canvas.height);
	for (let unit = 0; unit < units.length; unit += 1) {
		const entry = units[unit];
		if (entry === undefined) { continue; }
		for (let py = 0; py < scale; py += 1) {
			const y = unit * scale + py;
			if (entry.type === 'fold') {
				for (let x = 0; x < canvas.width; x += 1) {
					const [red, green, blue] = (x + y) % period < half ? HEAP_FOLD_HATCH : HEAP_BACKGROUND;
					const pixel = (y * canvas.width + x) * 4;
					image.data[pixel] = red; image.data[pixel + 1] = green; image.data[pixel + 2] = blue; image.data[pixel + 3] = 255;
				}
				continue;
			}
			for (let column = 0; column < columns; column += 1) {
				const cell = entry.row * columns + column;
			const index = owner[cell];
			const [red, green, blue] = index < 0 ? HEAP_BACKGROUND : colourOf(map.blocks[index] ?? [0, 0, false, null], map);
				// Reason: each shared edge is owned by its right/lower cell
				// alone, mirroring the PNG rule, so the line stays two pixels
				// and every right angle it meets stays closed.
				const left = column > 0 ? owner[cell - 1] : -1;
				const up = entry.row > 0 ? owner[cell - columns] : -1;
				for (let px = 0; px < scale; px += 1) {
					const divider = index >= 0 && ((column > 0 && left >= 0 && left !== index && px < 2) ||
						(entry.row > 0 && up >= 0 && up !== index && py < 2));
					const [r, g, b] = divider ? HEAP_BACKGROUND : [red, green, blue];
					const pixel = (y * canvas.width + column * scale + px) * 4;
					image.data[pixel] = r; image.data[pixel + 1] = g; image.data[pixel + 2] = b; image.data[pixel + 3] = 255;
				}
			}
		}
	}
	// Reason: a wrapping block's inner corner joins a left bar from above
	// with a top bar from the left; without this pixel the two meet only
	// diagonally and the right angle reads open. Mirrors `cornerPoints`.
	const atCell = (r: number, c: number): number => {
		if (r < 0 || r >= rows || c < 0 || c >= columns) { return -1; }
		return owner[r * columns + c];
	};
	const hasLeftBar = (r: number, c: number): boolean => {
		if (c <= 0) { return false; }
		const index = atCell(r, c);
		const left = atCell(r, c - 1);
		return index >= 0 && left >= 0 && left !== index;
	};
	const hasTopBar = (r: number, c: number): boolean => {
		if (r <= 0) { return false; }
		const index = atCell(r, c);
		const up = atCell(r - 1, c);
		return index >= 0 && up >= 0 && up !== index;
	};
	for (let r = 0; r <= rows; r += 1) {
		for (let c = 0; c <= columns; c += 1) {
			if (!hasLeftBar(r - 1, c) || !hasTopBar(r, c - 1)) { continue; }
			for (let dy = 0; dy < 2; dy += 1) {
				for (let dx = 0; dx < 2; dx += 1) {
					const pixel = ((r * scale + dy) * canvas.width + c * scale + dx) * 4;
					image.data[pixel] = HEAP_BACKGROUND[0];
					image.data[pixel + 1] = HEAP_BACKGROUND[1];
					image.data[pixel + 2] = HEAP_BACKGROUND[2];
					image.data[pixel + 3] = 255;
				}
			}
		}
	}
	context.putImageData(image, 0, 0);

	paintHeapLegend(legend, map, rows, scale);
	paintHeapAddrs(addrStart, addrEnd, map);

	// Reason: assigning the handler replaces the previous one, so a redraw never
	// stacks listeners on the same canvas.
	wireHeapGrip(container, grip, grid);
	canvas.onmousemove = (event: MouseEvent): void => {
		if (pan !== null && pan.moved) { hideTooltip(); return; }
		if (heapResize !== null) { hideTooltip(); return; }
		const rect = canvas.getBoundingClientRect();
		if (rect.width <= 0 || rect.height <= 0 || tooltip === null) { return; }
		const x = (event.clientX - rect.left) / rect.width * canvas.width;
		const y = (event.clientY - rect.top) / rect.height * canvas.height;
		const at = Math.floor(y / scale);
		const entry = units[at];
		if (entry === undefined) { hideTooltip(); return; }
		let key: string;
		let anchorLeft: number;
		let anchorRight: number;
		let anchorTop: number;
		const unitHeight = rect.height / units.length;
		if (entry.type === 'fold') {
			key = `f:${entry.fold.row}`;
			anchorLeft = rect.left;
			anchorRight = rect.right;
			anchorTop = rect.top + at * unitHeight;
			paintHeapLabel(tooltip, { kind: 'fold', fold: entry.fold });
		} else {
			const column = Math.floor(x / scale);
			if (column < 0 || column >= columns) { hideTooltip(); return; }
			const index = owner[entry.row * columns + column];
			if (index < 0) {
				key = `g:${entry.row * columns + column}`;
			paintHeapLabel(tooltip, { kind: 'gap' });
			} else {
				key = `b:${index}`;
				paintHeapLabel(tooltip, { kind: 'block', block: map.blocks[index] ?? [0, 0, false, null], owners: map.owners });
			}
			const cellWidth = rect.width / columns;
			anchorLeft = rect.left + column * cellWidth;
			anchorRight = anchorLeft + cellWidth;
			anchorTop = rect.top + at * unitHeight;
		}
		if (key === pinnedKey) { return; }
		pinnedKey = key;
		// Reason: the custom floating tooltip is the single hover surface. A
		// native `title` here would make Chromium show a second label beside it.
		canvas.removeAttribute('title');
		tooltip.hidden = false;
		const labelWidth = tooltip.offsetWidth;
		const labelHeight = tooltip.offsetHeight;
		const right = anchorRight + 8;
		const left = right + labelWidth > window.innerWidth - 8 ? Math.max(4, anchorLeft - 8 - labelWidth) : Math.max(4, right);
		tooltip.style.left = `${left}px`;
		tooltip.style.top = `${Math.max(4, Math.min(anchorTop, window.innerHeight - labelHeight - 8))}px`;
	};
	canvas.onmouseleave = (event: MouseEvent): void => {
		// Reason: travelling onto the pinned label must not hide it; the
		// label covers canvas cells whose hover would otherwise take over.
		if (event.relatedTarget instanceof Node && tooltip !== null && tooltip.contains(event.relatedTarget)) { return; }
		hideTooltip();
	};
	if (tooltip !== null) {
		tooltip.onmouseleave = (event: MouseEvent): void => {
			if (event.relatedTarget instanceof Node && canvas.contains(event.relatedTarget)) { return; }
			hideTooltip();
		};
	}
	canvas.onpointermove = canvas.onmousemove;
	canvas.onpointerleave = canvas.onmouseleave;
	if (viewport !== null) {
		// Reason: tall bitmaps scroll inside the capped viewport; dragging
		// pans them without moving the surrounding card.
		viewport.onpointerdown = (event: PointerEvent): void => {
			if (event.button !== 0) { return; }
			pan = { x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop, moved: false };
		};
		viewport.onpointermove = (event: PointerEvent): void => {
			if (pan === null) { return; }
			const dx = event.clientX - pan.x;
			const dy = event.clientY - pan.y;
			if (!pan.moved && Math.hypot(dx, dy) < 4) { return; }
			pan.moved = true;
			viewport.scrollLeft = pan.left - dx;
			viewport.scrollTop = pan.top - dy;
			hideTooltip();
			viewport.classList.add('gdr-heap-panning');
		};
		const endPan = (): void => {
			pan = null;
			viewport.classList.remove('gdr-heap-panning');
		};
		viewport.onpointerup = endPan;
		viewport.onpointercancel = endPan;
		viewport.onscroll = hideTooltip;
	}
	wireHeapCorner(container, canvas, columns, units.length, scale);
	const body = bodyOf(container);
	if (body !== null) { body.onscroll = hideTooltip; }
}

/**
 * Display size from the zoom factor: the backing store never changes, so a
 * drag repaints nothing and the hover math (rect-based) stays exact.
 */
function sizeHeapCanvas(canvas: HTMLCanvasElement, columns: number, units: number, scale: number): void {
	const display = Math.max(2, Math.round(scale * heapViewZoom));
	canvas.style.width = `${columns * display}px`;
	canvas.style.height = `${units * display}px`;
}

/**
 * The bottom-right corner grip: drag it to scale the bitmap uniformly.
 * Double-click resets to full size.
 */
function wireHeapCorner(container: HTMLElement, canvas: HTMLCanvasElement, columns: number, units: number, scale: number): void {
	const corner = container.querySelector<HTMLElement>('[data-heap-corner]');
	if (corner === null) { return; }
	corner.onpointerdown = (event: PointerEvent): void => {
		if (event.button !== 0) { return; }
		event.preventDefault();
		if (typeof corner.setPointerCapture === 'function') {
			try { corner.setPointerCapture(event.pointerId); } catch { /* a detached grip needs no capture */ }
		}
		const rect = canvas.getBoundingClientRect();
		heapCornerDrag = {
			container,
			originX: rect.left,
			originY: rect.top,
			baseW: columns * scale,
			baseH: units * scale
		};
	};
	corner.ondblclick = (): void => {
		heapCornerDrag = null;
		heapViewZoom = 1;
		sizeHeapCanvas(canvas, columns, units, scale);
	};
}

/**
 * The right-edge grip: drag it to re-lay the bitmap at another column
 * count; the host replays the producer layout, so folds track the width.
 * Double-click resets to the envelope grid.
 */
function wireHeapGrip(container: HTMLElement, grip: HTMLElement | null, grid: ClientGrid): void {
	if (grip === null) { return; }
	const cellPx = Math.max(6, grid.scale);
	grip.onpointerdown = (event: PointerEvent): void => {
		if (event.button !== 0) { return; }
		event.preventDefault();
		hideHeapResize(container);
		heapResize = {
			container,
			startX: event.clientX,
			startColumns: grid.width,
			x: event.clientX,
			y: event.clientY,
			cellPx,
			minCols: Math.ceil(200 / cellPx)
		};
	};
	grip.ondblclick = (): void => {
		const request: ClickMessage = { type: 'resetHeapSize' };
		withSession(request, grip);
		api.postMessage(request);
		heapResize = null;
		heapViewGrid = null;
		heapViewRowBytes = 0;
		hideHeapResize(container);
		drawHeapCanvas(container);
	};
}

/** Show the live drag label: folded bytes plus average row bytes. */
function placeResizeLabel(container: HTMLElement, x: number, y: number): void {
	const label = container.querySelector<HTMLElement>('[data-heap-resize]');
	if (label === null) { return; }
	label.textContent = `row bytes: ${heapViewRowBytes} B`;
	label.hidden = false;
	const labelWidth = label.offsetWidth;
	const labelHeight = label.offsetHeight;
	label.style.left = `${Math.max(4, Math.min(x + 12, window.innerWidth - labelWidth - 8))}px`;
	label.style.top = `${Math.max(4, Math.min(y + 16, window.innerHeight - labelHeight - 8))}px`;
}

/** Hide the resize label wherever it lives. */
function hideHeapResize(container: HTMLElement): void {
	const label = container.querySelector<HTMLElement>('[data-heap-resize]');
	if (label !== null) { label.hidden = true; }
}

/** Used-area start/end labels above and below the bitmap, mirroring the PNG strips. */
function paintHeapAddrs(start: HTMLElement | null, end: HTMLElement | null, map: ClientHeapMap): void {
	// Reason: textContent, never innerHTML: addresses ride target data paths.
	if (start !== null) { start.textContent = `start: ${heapAddress(map.base)}`; }
	if (end !== null) { end.textContent = `end: ${heapAddress(map.end)}`; }
}

/**
 * The occupancy ranking right of the bitmap: one vertical list, or two when
 * it outgrows twice the used height, mirroring the PNG legend's single wrap.
 */
function paintHeapLegend(legend: HTMLElement | null, map: ClientHeapMap, rows: number, baseScale: number): void {
	if (legend === null) { return; }
	legend.replaceChildren();
	const entries = heapLegend(map);
	// Reason: the wrap budget follows the base cell size; 20px rows match
	// the PNG legend's line step exactly, and
	// short rankings stay in one column like the PNG below eight rows.
	const budget = 2 * rows * baseScale;
	const lanes = entries.length > HEAP_LEGEND_WRAP_MIN && 12 + entries.length * 20 > budget ? 2 : 1;
	const perLane = Math.ceil(entries.length / lanes);
	for (let lane = 0; lane < lanes; lane += 1) {
		const column = document.createElement('div');
		column.className = 'gdr-heap-legend-col';
		for (const entry of entries.slice(lane * perLane, (lane + 1) * perLane)) {
			column.append(heapLegendRow(entry));
		}
		legend.append(column);
	}
}

/** One legend row: the swatch block and the occupancy text beside it. */
function heapLegendRow(entry: HeapLegendRow): HTMLElement {
	const row = document.createElement('div');
	row.className = 'gdr-heap-legend-row';
	const swatch = document.createElement('span');
	swatch.className = 'gdr-heap-legend-swatch';
	// Reason: element.style is not markup, so the CSP does not block it.
	swatch.style.background = entry.colour;
	// Reason: textContent, never innerHTML: an owner name is target data.
	const text = document.createElement('span');
	text.textContent = entry.text;
	row.append(swatch, text);
	return row;
}
