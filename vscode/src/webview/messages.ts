/**
 * Messages the webview posts back to the host, decoded at the I/O boundary.
 *
 * Kept free of `vscode` imports so the decoder is a plain function the node
 * test runner can exercise. Anything that does not match one of the shapes
 * below is dropped: the host acts only on well-formed requests.
 */

/** A table's place in the panel: its order, its column widths and the columns shown. */
export interface TableLayout {
	/** Table kind (`task`, `timer`, `heap`, `semaphore`, ...). */
	kind: string;
	/** Column key -> width in CSS pixels, for the columns the reader resized. */
	widths: Record<string, number>;
	/** Column keys the reader turned off in the child checkboxes. */
	hiddenColumns: string[];
	/** Saved header order; optional for layouts saved by older versions. */
	columnOrder?: string[];
}

/** The whole panel layout: table order and per-table state, saved per workspace. */
export interface PanelLayout {
	/** Table kinds in the order the reader arranged them. */
	order: string[];
	/** Table kinds the reader turned off in the parent checkboxes. */
	hiddenTables: string[];
	tables: Record<string, TableLayout>;
}

export type WebviewMessage =
	| { type: 'openMemory'; address: string; sessionId?: string }
	| {
			type: 'openSource';
			file: string;
			name: string;
			line: number;
			/** The address to show in the memory view when no source file resolves. */
			fallbackAddress?: string;
			sessionId?: string;
	  }
	| { type: 'selectSession'; sessionId: string }
	| { type: 'heapDump'; sessionId?: string }
	| { type: 'resizeHeap'; columns: number; sessionId?: string }
	| { type: 'resetHeapSize'; sessionId?: string }
	| { type: 'layout'; layout: PanelLayout }
	| { type: 'refresh'; sessionId?: string };

/** An absolute path as GDB reports it: POSIX or a Windows drive letter. */
const ABSOLUTE_PATH = /^(\/|[A-Za-z]:[\\/])/;

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function sessionOf(payload: Record<string, unknown>): { sessionId?: string } {
	return typeof payload.sessionId === 'string' ? { sessionId: payload.sessionId } : {};
}

/** A width is a finite, positive pixel count; anything else is dropped. */
function widthsOf(value: unknown): Record<string, number> {
	const widths: Record<string, number> = {};
	if (!isRecord(value)) {
		return widths;
	}
	for (const [key, width] of Object.entries(value)) {
		if (typeof width === 'number' && Number.isFinite(width) && width > 0) {
			widths[key] = Math.round(width);
		}
	}
	return widths;
}

function stringsOf(value: unknown): string[] {
	return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

/**
 * Decode a layout message. Unknown table kinds are kept only as far as they
 * are strings: the renderer ignores a kind it does not draw, so a stale saved
 * layout cannot break the panel.
 */
export function decodeLayout(value: unknown): PanelLayout | null {
	if (!isRecord(value)) {
		return null;
	}
	const tables: Record<string, TableLayout> = {};
	if (isRecord(value.tables)) {
		for (const [kind, raw] of Object.entries(value.tables)) {
			if (!isRecord(raw)) {
				continue;
			}
			tables[kind] = {
				kind,
				widths: widthsOf(raw.widths),
				hiddenColumns: stringsOf(raw.hiddenColumns),
				...(Array.isArray(raw.columnOrder) ? { columnOrder: [...new Set(stringsOf(raw.columnOrder))] } : {})
			};
		}
	}
	return {
		order: stringsOf(value.order),
		hiddenTables: stringsOf(value.hiddenTables),
		tables
	};
}

export function decodeWebviewMessage(value: unknown): WebviewMessage | null {
	if (!isRecord(value)) {
		return null;
	}
	const session = sessionOf(value);
	if (value.type === 'selectSession' && typeof value.sessionId === 'string') {
		return { type: 'selectSession', sessionId: value.sessionId };
	}
	if (value.type === 'openMemory' && typeof value.address === 'string') {
		return { type: 'openMemory', address: value.address, ...session };
	}
	if (
		value.type === 'openSource' &&
		typeof value.file === 'string' &&
		ABSOLUTE_PATH.test(value.file) &&
		typeof value.line === 'number' &&
		Number.isInteger(value.line) &&
		value.line >= 1
	) {
		// Reason: the name is the path as the debug information recorded it; an
		// absent or non-string name falls back to the GDB file, never to nothing.
		const name = typeof value.name === 'string' && value.name !== '' ? value.name : value.file;
		const fallback = typeof value.fallbackAddress === 'string' ? { fallbackAddress: value.fallbackAddress } : {};
		return { type: 'openSource', file: value.file, name, line: value.line, ...fallback, ...session };
	}
	if (value.type === 'heapDump') {
		return { type: 'heapDump', ...session };
	}
	if (value.type === 'resizeHeap' && typeof value.columns === 'number' && Number.isInteger(value.columns) && value.columns > 0) {
		return { type: 'resizeHeap', columns: value.columns, ...session };
	}
	if (value.type === 'resetHeapSize') {
		return { type: 'resetHeapSize', ...session };
	}
	if (value.type === 'refresh') {
		return { type: 'refresh', ...session };
	}
	if (value.type === 'layout') {
		const layout = decodeLayout(value.layout);
		return layout === null ? null : { type: 'layout', layout };
	}
	return null;
}
