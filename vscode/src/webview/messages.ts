/**
 * Messages the webview posts back to the host, decoded at the I/O boundary.
 *
 * Kept free of `vscode` imports so the decoder is a plain function the node
 * test runner can exercise. Anything that does not match one of the shapes
 * below is dropped: the host acts only on well-formed requests.
 */

export type WebviewMessage =
	| { type: 'openMemory'; address: string; sessionId?: string }
	| { type: 'openSource'; file: string; line: number; sessionId?: string };

/** An absolute path as GDB reports it: POSIX or a Windows drive letter. */
const ABSOLUTE_PATH = /^(\/|[A-Za-z]:[\\/])/;

export function decodeWebviewMessage(value: unknown): WebviewMessage | null {
	if (typeof value !== 'object' || value === null) {
		return null;
	}
	const payload = value as Record<string, unknown>;
	const sessionId = typeof payload.sessionId === 'string' ? payload.sessionId : undefined;
	const session = sessionId !== undefined ? { sessionId } : {};
	if (payload.type === 'openMemory' && typeof payload.address === 'string') {
		return { type: 'openMemory', address: payload.address, ...session };
	}
	if (
		payload.type === 'openSource' &&
		typeof payload.file === 'string' &&
		ABSOLUTE_PATH.test(payload.file) &&
		typeof payload.line === 'number' &&
		Number.isInteger(payload.line) &&
		payload.line >= 1
	) {
		return { type: 'openSource', file: payload.file, line: payload.line, ...session };
	}
	return null;
}
