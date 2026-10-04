/**
 * The webview bootstrap: swap host-rendered HTML and wire clicks back.
 *
 * This file must stay import-free: the webview loads it as a classic script,
 * and the extension has no bundler yet, so it cannot `require` anything. The
 * host renders the whole panel (see `render.ts`) and posts the markup; the
 * only data flowing back is a click on an address or a refresh request.
 */

/// <reference lib="dom" />
// Reason: the main tsconfig targets the extension host (lib ES2022 only);
// this file runs inside the webview where the DOM is the runtime, so it
// pulls the DOM lib in locally rather than loosening the whole project.

declare function acquireVsCodeApi(): {
	postMessage(message: unknown): void;
};

interface ClickMessage {
	type: 'openMemory';
	sessionId?: string;
	address: string;
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
	const address = closestAddress(target);
	if (address === null) {
		return;
	}
	event.preventDefault();
	const message: ClickMessage = { type: 'openMemory', address };
	const sessionId = closestSession(target);
	if (sessionId !== null) {
		message.sessionId = sessionId;
	}
	api.postMessage(message);
});

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
	const fragment = document.createRange().createContextualFragment(html);
	container.replaceChildren(fragment);
}
