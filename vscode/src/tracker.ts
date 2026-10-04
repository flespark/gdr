/**
 * Resolving the `debug-tracker-vscode` API out of what that extension exports.
 *
 * The tracker publishes two shapes and a client must accept both:
 *
 * 1. its activated exports object *is* the shared tracker instance (the
 *    0.0.15 generation installed alongside cortex-debug), and
 * 2. newer releases carry the client library's `DebugTrackerFactory`, whose
 *    static `getTrackerExtension` activates the service itself.
 *
 * Calling shape 1's object as if it were shape 2 throws inside activation,
 * which the host reports as a failed activation: no output channel, no panel.
 * So the choice is a pure function here, unit-tested, and it reports the shape
 * it saw instead of throwing.
 */

/** The versioned v1 subscription request the tracker accepts. */
export interface TrackerSubscriptionRequest<E = unknown> {
	version: number;
	body: {
		debuggers: string[];
		wantCurrentStatus: boolean;
		notifyAllEvents: boolean;
		debugLevel: number;
		handler: (event: E) => void;
	};
}

/** What the tracker answers a subscription with: a grant, or a refusal string. */
export type TrackerGrant = { unsubscribe?(): void } | string;

/** The minimum a resolved tracker handle must offer. */
export interface TrackerHandle<E = unknown> {
	subscribe(request: TrackerSubscriptionRequest<E>): TrackerGrant;
	unsubscribe?(): void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null;
}

function isTrackerHandle(value: unknown): value is TrackerHandle {
	return isRecord(value) && typeof value.subscribe === 'function';
}

/** Name the value's shape for a diagnostic line without dumping it. */
export function describeValue(value: unknown): string {
	if (value === null) {
		return 'null';
	}
	if (Array.isArray(value)) {
		return 'array';
	}
	const kind = typeof value;
	if (kind !== 'object') {
		return kind;
	}
	const keys = Object.keys(value as object);
	return keys.length === 0
		? 'object with no keys'
		: `object with keys [${keys.slice(0, 8).join(', ')}]`;
}

/** What the tracker sends its subscribers: the session object is optional. */
export interface TrackerEventLike<S = unknown> {
	sessionId?: string;
	session?: S;
}

/**
 * Find the session a tracker event belongs to.
 *
 * The tracker addresses sessions by id (`sessionId`) and only attaches the
 * session object to some payloads, so an event whose session cannot be
 * resolved is dropped by the caller rather than attributed to the wrong one.
 *
 * @param event Payload the tracker delivered.
 * @param lookup Resolve a live session from its id.
 */
export function resolveEventSession<S>(
	event: TrackerEventLike<S>,
	lookup: (id: string) => S | undefined
): S | undefined {
	if (event.session !== undefined) {
		return event.session;
	}
	if (typeof event.sessionId !== 'string') {
		return undefined;
	}
	return lookup(event.sessionId);
}

/**
 * Resolve the shared tracker handle from an activated exports value.
 *
 * Never throws: an unreachable or reshaped API comes back as an `Error` whose
 * message says what the extension did export.
 *
 * @param activated Value returned by activating `debug-tracker-vscode`.
 * @param clientName Extension name the tracker reports to (its client id).
 */
export async function resolveTrackerApi<T extends TrackerHandle = TrackerHandle>(
	activated: unknown,
	clientName = 'gdb-rtos-views'
): Promise<T | Error> {
	if (isTrackerHandle(activated)) {
		// SAFETY: the predicate checked the one member callers use; `T` is the
		// caller's narrower view of this same object, never a different value.
		return activated as unknown as T;
	}
	const factory = isRecord(activated) ? activated.DebugTrackerFactory : undefined;
	if (isRecord(factory) && typeof factory.getTrackerExtension === 'function') {
		try {
			const handle: unknown = await factory.getTrackerExtension.call(factory, clientName);
			if (isTrackerHandle(handle)) {
				// SAFETY: as above -- the same predicate is the whole contract.
				return handle as unknown as T;
			}
			return handle instanceof Error
				? handle
				: new Error(`debug-tracker-vscode returned no tracker (${describeValue(handle)})`);
		} catch (error) {
			return error instanceof Error ? error : new Error(String(error));
		}
	}
	return new Error(
		`debug-tracker-vscode exports neither a tracker instance nor DebugTrackerFactory (${describeValue(activated)})`
	);
}
