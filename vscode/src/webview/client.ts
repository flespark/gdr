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
	type: 'openMemory' | 'openSource';
	sessionId?: string;
	address?: string;
	file?: string;
	line?: number;
}

/** The scroll positions a host refresh would otherwise reset to the top. */
interface ScrollMemory {
	session: string | null;
	top: number;
	/** Horizontal offsets of the table scrollers, in document order. */
	tables: number[];
}

const api = acquireVsCodeApi();
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
	const payload = message as { type?: unknown; html?: unknown };
	if (payload.type === 'state' && typeof payload.html === 'string') {
		applyTrustedHostMarkup(root, payload.html);
	}
});

document.addEventListener('click', (event: MouseEvent) => {
	const target = event.target as HTMLElement | null;
	const source = closestData(target, 'data-source-file');
	if (source !== null) {
		event.preventDefault();
		const message: ClickMessage = {
			type: 'openSource',
			file: source.dataset.sourceFile ?? '',
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
	return Array.from(container.querySelectorAll<HTMLElement>('.gdr-scroll'));
}

/** Record where the reader is before the markup is replaced. */
function captureScroll(container: HTMLElement): ScrollMemory {
	const body = bodyOf(container);
	return {
		session: body?.dataset.session ?? null,
		top: body?.scrollTop ?? 0,
		tables: tablesOf(container).map((table) => table.scrollLeft)
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
	tablesOf(container).forEach((table, index) => {
		const left = memory.tables[index];
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
	const memory = captureScroll(container);
	const fragment = document.createRange().createContextualFragment(html);
	container.replaceChildren(fragment);
	restoreScroll(container, memory);
}
