/**
 * Extension entry point: wire debug sessions to their GDR helper.
 *
 * Responsibilities kept here (and not in `session.ts`): reading settings,
 * loading the runtime path, injecting the Python gate and loader through each
 * adapter's launch configuration, and following the debug tracker's stop
 * events so a session's lifecycle starts at the first stop -- GDR is never
 * driven while the target runs.
 */

/// <reference types="node" />
// Reason: this subproject keeps its own node_modules, so an editor or `tsc`
// invocation whose analysis root is the repository root would otherwise not
// resolve the Node builtins this file uses.

import { randomBytes } from 'node:crypto';
import * as os from 'node:os';
import * as path from 'node:path';

import * as vscode from 'vscode';

import {
	injectionPlan,
	supportedDebugTypes,
	trackedDebugTypes,
	unsupportedReason
} from './inject.ts';
import { RtosViewsPanel, type HeapMapFetcher, type LayoutStore, type SnapshotFetcher } from './panel.ts';
import { GdrCache, GdrSession, runtimeFor } from './session.ts';
import { costArgs, parseSnapshotTransport, type CostOptions } from './snapshot.ts';
import { decodeLayout, type PanelLayout } from './webview/messages.ts';
import {
	resolveEventSession,
	resolveTrackerApi,
	type TrackerHandle
} from './tracker.ts';
import type { GdrSettings } from './probe.ts';

/**
 * The debug types the tracker is asked about.
 *
 * `inject.ts` is the one source for which sessions GDR drives: the injection
 * plan and this subscription can never disagree about them. The union also
 * carries the types GDR cannot host, so those sessions reach the panel and are
 * told why instead of never being seen at all.
 */
const TRACKED_TYPES = trackedDebugTypes();

/** A granted tracker subscription; the tracker answers with a string on refusal. */
interface TrackerGrant {
	unsubscribe?(): void;
}

/** The subscription request this extension sends to the tracker. */
interface TrackerRequest {
	version: number;
	body: {
		debuggers: string[];
		wantCurrentStatus: boolean;
		notifyAllEvents: boolean;
		debugLevel: number;
		handler: (event: TrackerEvent) => void;
	};
}

interface TrackerSubscription extends TrackerHandle<TrackerEvent> {
	subscribe(request: TrackerRequest): TrackerGrant | string;
}

/**
 * Event names `debug-tracker-vscode`'s v1 API emits (see its `exports.ts`).
 *
 * They are lowercase/hyphenated at runtime, which the previous string
 * literals got wrong (`'Terminated'`, `'FirstStackTrace'`), silently making
 * the whole stop lifecycle dead. Naming them as a union turns that class of
 * typo into a compile error, because a `case` label outside the union is
 * rejected.
 */
type TrackerEventName =
	| 'unknown'
	| 'initializing'
	| 'started'
	| 'stopped'
	| 'running'
	| 'terminated'
	| 'first-stack-trace'
	| 'capabilities'
	| 'protocol-event';

/** Status changes come from `DebugSessionStatus`; the frame id rides the one below. */
interface TrackerEvent {
	event?: TrackerEventName;
	session?: vscode.DebugSession;
	sessionId?: string;
	stackTrace?: { body?: { stackFrames?: { id?: number }[] } };
}

/** The key the panel layout is saved under; one per workspace, not per session. */
const LAYOUT_KEY = 'gdr.panel.layout';

/**
 * The panel layout kept in the workspace's memento.
 *
 * A stored value that no longer decodes (an older shape, a hand edit) reads as
 * no layout: the panel then starts from the defaults rather than failing.
 */
export function workspaceLayoutStore(store: vscode.Memento): LayoutStore {
	return {
		read: (): PanelLayout | null => decodeLayout(store.get<unknown>(LAYOUT_KEY)),
		write: (layout: PanelLayout): void => {
			void store.update(LAYOUT_KEY, layout);
		}
	};
}

let sessionFactory: ((session: vscode.DebugSession, frameId: number) => Promise<boolean>) | undefined;

export function activate(context: vscode.ExtensionContext): void {
	const output = vscode.window.createOutputChannel('GDB RTOS Views');
	context.subscriptions.push(output);
	const log = (message: string): void => {
		output.appendLine(`[gdr] ${message}`);
	};

	const cache = new GdrCache(context.workspaceState);
	const sessions = new Map<string, GdrSession>();

	// Reason: the tracker addresses sessions by id and attaches the session
	// object to only some payloads (its `notifyCurrentStatus`), so the extension
	// keeps its own id -> session map for the stop events that carry an id only.
	const liveSessions = new Map<string, vscode.DebugSession>();
	context.subscriptions.push(
		vscode.debug.onDidStartDebugSession((session) => {
			liveSessions.set(session.id, session);
		}),
		vscode.debug.onDidTerminateDebugSession((session) => {
			liveSessions.delete(session.id);
		})
	);
	// Reason: onDidStartDebugSession is not guaranteed to fire before the adapter's
	// first tracker events, so any payload that carries the session object also
	// registers it; a later id-only stop event then still resolves.
	const rememberSession = (session: vscode.DebugSession): void => {
		liveSessions.set(session.id, session);
	};
	const sessionById = (id: string): vscode.DebugSession | undefined => {
		const known = liveSessions.get(id);
		if (known !== undefined) {
			return known;
		}
		const active = vscode.debug.activeDebugSession;
		return active !== undefined && active.id === id ? active : undefined;
	};

	const config = (): vscode.WorkspaceConfiguration => vscode.workspace.getConfiguration('gdr');

	const costOptions = (): CostOptions => ({
		noHeap: config().get<boolean>('refreshHeaps', true) === false,
		noDetails: config().get<boolean>('refreshDetails', true) === false
	});

	const recordDir = (): string => config().get<string>('recordSnapshotsDir', '');

	const settings = (): GdrSettings => {
		return {
			rtos: config().get<string>('rtos', ''),
			version: config().get<string>('version', ''),
			autoInit: config().get<boolean>('autoInit', true)
		};
	};

	const snapshotFetcher: SnapshotFetcher = async (sessionId, frameId) => {
		const gs = sessions.get(sessionId);
		if (gs === undefined) {
			return null;
		}
		const args = costArgs(costOptions());
		// Reason: the file is written where GDB runs, so the bypass only holds
		// when the extension can read that same filesystem -- normally true
		// (GDB is started locally, whatever host gdbserver is on) but not for
		// a GDB in a separate filesystem namespace such as WSL.
		if (parseSnapshotTransport(config().get<unknown>('snapshotTransport')) === 'file') {
			return gs.fetchSnapshotFile(frameId, args, recordDir());
		}
		return gs.fetch('snapshot', args, frameId, { dir: recordDir() });
	};

	// Reason: the heap map is one more verb over the same session transport as the
	// snapshot, so a chunked or file-routed answer is handled identically.
	const heapMapFetcher: HeapMapFetcher = async (sessionId, frameId) => {
		const gs = sessions.get(sessionId);
		if (gs === undefined) {
			return null;
		}
		return gs.fetch('heap-dump', [], frameId);
	};

	const panel = new RtosViewsPanel({
		log,
		extensionUri: context.extensionUri,
		costOptions,
		recordDir,
		fetchSnapshot: snapshotFetcher,
		fetchHeapMap: heapMapFetcher,
		layoutStore: workspaceLayoutStore(context.workspaceState)
	});
	context.subscriptions.push(
		vscode.window.registerWebviewViewProvider(RtosViewsPanel.viewType, panel, {
			webviewOptions: { retainContextWhenHidden: true }
		})
	);

	const sessionFor = (session: vscode.DebugSession): GdrSession => {
		let existing = sessions.get(session.id);
		if (existing === undefined) {
			const runtime = runtimeFor(context.extensionUri, vscode.workspace.getConfiguration('gdr').get<string>('runtimePath', ''));
			existing = new GdrSession(session, runtime, settings(), log, cache);
			sessions.set(session.id, existing);
		}
		return existing;
	};

	sessionFactory = async (session, frameId) => sessionFor(session).onFirstStop(frameId);

	// Load GDR during launch, after a Python check that does not abort the
	// session, so the helper exists before the first expression a user types.
	context.subscriptions.push(
		vscode.debug.registerDebugConfigurationProvider('*', {
			resolveDebugConfiguration: (folder, config) => {
				if (!supportedDebugTypes().includes(config.type)) {
					return config;
				}
				const runtime = runtimeFor(context.extensionUri, vscode.workspace.getConfiguration('gdr').get<string>('runtimePath', ''));
				const loaderPath = path.join(os.tmpdir(), `gdr-load-${randomBytes(8).toString('hex')}.gdb`);
				config.gdrLoaderPath = loaderPath;
				const plan = injectionPlan(config.type, config.request === 'attach' ? 'attach' : 'launch', {
					runtimePath: runtime.directory,
					loaderPath,
					init: null
				});
				for (const entry of plan.entries) {
					const existing = config[entry.key];
					config[entry.key] = Array.isArray(existing) ? [...existing, ...(entry.value as unknown[])] : entry.value;
				}
				void folder;
				return config;
			}
		})
	);

	context.subscriptions.push(
		vscode.commands.registerCommand('gdrRtosViews.showStatus', () => {
			const active = vscode.debug.activeDebugSession;
			if (!active) {
				void vscode.window.showWarningMessage('GDR: no active debug session');
				return;
			}
			const state = sessionFor(active);
			const decision = state.decision;
			if (state.initialized) {
				log(`GDR: ${state.initialized.rtos} ${state.initialized.version}`);
				output.show(true);
			} else {
				void vscode.window.showWarningMessage(`GDR: not initialized (${decision?.kind ?? 'no decision yet'})`);
			}
		}),
		vscode.commands.registerCommand('gdrRtosViews.refresh', () => {
			panel.refreshNow(undefined);
		}),
		vscode.commands.registerCommand('gdrRtosViews.open', () =>
			vscode.commands.executeCommand('gdrRtosViews.panel.focus')
		)
	);

	// Reason: the view is hidden until a session GDR can drive exists, so the
	// panel is where the debugging views already live instead of a standing
	// icon; `panel.sessionStarted`/`sessionEnded` flip this key.
	void vscode.commands.executeCommand('setContext', 'gdrRtosViews.active', false);

	void subscribeToTracker({
		context,
		panel,
		config,
		sessions,
		sessionFor,
		sessionById,
		rememberSession,
		log
	});
}

/**
 * Follow stop events through `debug-tracker-vscode`.
 *
 * The tracker is a declared extension dependency; a client that cannot reach it
 * says so once instead of silently never starting.
 */
async function subscribeToTracker(deps: TrackerDeps): Promise<void> {
	const { context, log } = deps;
	const startupFailure = (reason: string): void => {
		log(reason);
		void vscode.window.showErrorMessage(`GDR: panel startup failed: ${reason}`);
	};
	const extension = vscode.extensions.getExtension('mcu-debug.debug-tracker-vscode');
	if (!extension) {
		startupFailure('debug-tracker-vscode is not installed; stop events will not reach GDR');
		return;
	}
	// Reason: a tracker whose exports do not match what this client expects used
	// to throw out of activation, which the host reports as a failed activation
	// and leaves the panel and the output channel missing entirely. Sampling the
	// API defensively keeps a protocol mismatch a logged degradation instead.
	try {
		const activated: unknown = await extension.activate();
		const tracker = await resolveTrackerApi<TrackerSubscription>(activated);
		if (tracker instanceof Error) {
			startupFailure(`debug tracker unavailable: ${tracker.message}`);
			return;
		}
		const grant = tracker.subscribe({
			version: 1,
			body: {
				debuggers: TRACKED_TYPES,
				wantCurrentStatus: true,
				notifyAllEvents: false,
				debugLevel: 0,
				handler: (event: TrackerEvent) => {
					// Reason: a rejection here would only reach the Extension Host console,
					// so the output channel would show a stall with no cause.
					handleTrackerEvent(event, deps).catch((error: unknown) => {
						const reason = `${event.event}: ${error instanceof Error ? error.message : String(error)}`;
						log(`tracker: ${reason}`);
						const session = resolveEventSession(event, deps.sessionById);
						if (session !== undefined && deps.sessions.has(session.id)) {
							deps.panel.sessionStarted(session.id, session.name);
							deps.panel.sessionFailed(session.id, reason);
						}
					});
				}
			}
		});
		// Reason: the tracker answers a refusal with a string instead of throwing
		// (`Unknown version`, `No handler specified`, ...), so an unchecked return
		// would report success for a subscription that delivers nothing.
		if (typeof grant === 'string') {
			startupFailure(`debug tracker refused the subscription: ${grant}`);
			return;
		}
		log('stop events follow debug-tracker-vscode');
	} catch (error) {
		startupFailure(`debug tracker subscription failed: ${error instanceof Error ? error.message : String(error)}`);
	}
	void context;
}

/** What the tracker handler needs from the activate closure. */
interface TrackerDeps {
	context: vscode.ExtensionContext;
	sessions: Map<string, GdrSession>;
	panel: RtosViewsPanel;
	config: () => vscode.WorkspaceConfiguration;
	/** Create or reuse the session object for a tracker-reported session. */
	sessionFor: (session: vscode.DebugSession) => GdrSession;
	/** Resolve a live debug session from the id a tracker payload carries. */
	sessionById: (id: string) => vscode.DebugSession | undefined;
	/** Record a session object a tracker payload carried, for later id-only events. */
	rememberSession: (session: vscode.DebugSession) => void;
	log: (message: string) => void;
}

async function handleTrackerEvent(event: TrackerEvent, deps: TrackerDeps): Promise<void> {
	const { sessions, panel, config, sessionFor } = deps;
	if (event.session !== undefined) {
		deps.rememberSession(event.session);
	}
	const session = resolveEventSession(event, deps.sessionById);
	if (session === undefined) {
		// Reason: the tracker's payloads carry the session object only on its
		// current-status events, so a stop event is attributed through its id and
		// dropped (with a trace) when no live session owns that id.
		deps.log(`tracker: ignored '${event.event}' (no live session yet)`);
		return;
	}
	switch (event.event) {
		case 'terminated':
			sessions.get(session.id)?.discardLaunchFiles();
			panel.sessionEnded(session.id);
			sessions.delete(session.id);
			return;
		case 'started': {
			// Reason: a session GDR cannot host gets a tab that says why. Without
			// this the types are never subscribed to and the user sees nothing at
			// all, which the README used to promise was a refusal with a reason.
			const refused = unsupportedReason(session.type);
			if (refused !== null) {
				panel.sessionStarted(session.id, session.name);
				panel.sessionFailed(session.id, refused, 'warning');
				deps.log(`${session.type}: ${refused}`);
				return;
			}
			panel.sessionStarted(session.id, session.name);
			const gs = sessionFor(session);
			void gs.watchLaunchPython().then((ok) => {
				if (!sessions.has(session.id) || ok || gs.errorMessage === null) {
					return;
				}
				panel.sessionFailed(session.id, gs.errorMessage, gs.failureLevel);
			}).catch((error: unknown) => {
				if (sessions.has(session.id)) {
					panel.sessionFailed(session.id, error instanceof Error ? error.message : String(error));
				}
			});
			return;
		}
		case 'running': {
			panel.targetRunning(session.id);
			return;
		}
		// Reason: the tracker resets its latch on every `stopped` event, so this
		// fires once per stop carrying that stop's frame id -- the frame id is
		// what an evaluate-based read needs, and per-stop is what makes the
		// panel refresh track the target.
		case 'first-stack-trace': {
			const frameId = event.stackTrace?.body?.stackFrames?.[0]?.id;
			if (typeof frameId !== 'number') {
				return;
			}
			// Reason: a refused session has no loader and no GDR to initialize;
			// the panel already carries its reason from `started`.
			if (unsupportedReason(session.type) !== null) {
				return;
			}
			panel.sessionStarted(session.id, session.name);
			const gs = sessionFor(session);
			if (!gs.initialized) {
				if (!sessionFactory) {
					return;
				}
				await sessionFactory(session, frameId);
				// Reason: initialization can await UI/transport while termination
				// removes this session. Never notify or revive its panel afterwards.
				if (sessions.get(session.id) !== gs) { return; }
				if (gs.initialized) {
					panel.sessionInitialized(session.id);
					if (config().get<boolean>('refreshOnStop', true)) {
						panel.firstStackTrace(session.id, frameId);
					}
				} else {
					panel.sessionFailed(session.id, gs.errorMessage ?? 'the session could not be initialized', gs.failureLevel);
				}
				return;
			}
			panel.sessionInitialized(session.id);
			if (config().get<boolean>('refreshOnStop', true)) {
				panel.firstStackTrace(session.id, frameId);
			}
			return;
		}
		default:
			// Reason: `event.event` is optional and the tracker also emits names
			// that carry no panel state (`capabilities`, `protocol-event`), so a
			// payload with nothing to update is a no-op, not an error.
			return;
	}
}

export function deactivate(): void {
	sessionFactory = undefined;
}
