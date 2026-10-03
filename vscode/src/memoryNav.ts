/**
 * Navigation from an address cell to the ecosystem memory viewer.
 *
 * mcu-debug's `memory-view` extension (the memory viewer shared by
 * cortex-debug and its siblings) opens a view from a URI of the form
 * `vscode://mcu-debug.memory-view/<expression>` and evaluates the expression
 * in the debugger it was started from. A bare hex literal is handled as an
 * address constant -- no inferior call, no side effect -- which is the only
 * target access this module ever induces. The scheme must come from the host
 * (`vscode.env.uriScheme`) because Insiders and VSCodium use different ones.
 */

/** Query keys the memory viewer understands; see its `MemviewUriOptions`. */
export interface MemoryViewOptions {
	/** The debug session to bind the view to, when not the active one. */
	sessionId?: string;
	/** A friendly name for the opened view. */
	displayName?: string;
}

/**
 * Build the external URI that opens the memory viewer at *address*.
 *
 * The address is embedded in the query as `expr` (a C hex literal), matching
 * what GDB evaluates; the path stays empty. The URI is meant for
 * `vscode.env.openExternal`, never for a browser.
 */
export function memoryViewUri(scheme: string, address: string, options: MemoryViewOptions = {}): string {
	const query = new URLSearchParams();
	query.set('expr', address);
	if (options.sessionId !== undefined && options.sessionId !== '') {
		query.set('sessionId', options.sessionId);
	}
	if (options.displayName !== undefined && options.displayName !== '') {
		query.set('displayName', options.displayName);
	}
	return `${scheme}://mcu-debug.memory-view?${query.toString()}`;
}

/** The extension id the URI above must find installed to handle it. */
export const MEMORY_VIEW_EXTENSION_ID = 'mcu-debug.memory-view';

/**
 * A human instruction shown when the memory viewer is not installed, naming
 * the id so the user can install exactly the right extension.
 */
export function memoryViewMissingHint(): string {
	return (
		`the address navigation needs the ${MEMORY_VIEW_EXTENSION_ID} extension, ` +
		`the memory viewer the mcu-debug debugging tools share`
	);
}
