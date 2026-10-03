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

import { injectionPlan } from './inject.ts';
import { RtosViewsPanel, type SnapshotFetcher } from './panel.ts';
import { GdrCache, GdrSession, runtimeFor } from './session.ts';
import { costArgs, parseSnapshotTransport, type CostOptions } from './snapshot.ts';
import type { GdrSettings } from './probe.ts';

/** Adapters whose launch configuration this extension appends the loader to. */
const SUPPORTED_TYPES = [
	'cortex-debug',
	'mcu-debug',
	'cppdbg',
	'gdbtarget',
	'stlinkgdbtarget',
	'jlinkgdbtarget'
];

/** Minimal shape of the `debug-tracker-vscode` API this extension consumes. */
interface DebugTrackerApi {
	getTrackerExtension(name: string): Promise<TrackerSubscription | Error>;
}

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

interface TrackerSubscription {
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

let sessionFactory: ((session: vscode.DebugSession, frameId: number) => Promise<boolean>) | undefined;

export function activate(context: vscode.ExtensionContext): void {
	const output = vscode.window.createOutputChannel('GDR RTOS Views');
	context.subscriptions.push(output);
	const log = (message: string): void => {
		output.appendLine(`[gdr] ${message}`);
	};

	const cache = new GdrCache(context.workspaceState);
	const sessions = new Map<string, GdrSession>();

	const config = (): vscode.WorkspaceConfiguration => vscode.workspace.getConfiguration('gdr');

	const costOptions = (): CostOptions => ({ noHeap: config().get<boolean>('refreshHeaps', true) === false });

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
		return gs.fetch('snapshot', args, frameId);
	};

	const panel = new RtosViewsPanel({
		log,
		extensionUri: context.extensionUri,
		costOptions,
		recordDir,
		fetchSnapshot: snapshotFetcher
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
				if (!SUPPORTED_TYPES.includes(config.type)) {
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
				void vscode.window.showInformationMessage('GDR: no active debug session');
				return;
			}
			const state = sessionFor(active);
			const decision = state.decision;
			void vscode.window.showInformationMessage(
				state.initialized
					? `GDR: ${state.initialized.rtos} ${state.initialized.version}`
					: `GDR: not initialized (${decision?.kind ?? 'no decision yet'})`
			);
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
	const extension = vscode.extensions.getExtension('mcu-debug.debug-tracker-vscode');
	if (!extension) {
		log('debug-tracker-vscode is not installed; stop events will not reach GDR');
		return;
	}
	const api = (await extension.activate()) as DebugTrackerApi;
	const tracker = await api.getTrackerExtension('gdr-rtos-views');
	if (tracker instanceof Error) {
		log(`debug tracker refused the subscription: ${tracker.message}`);
		return;
	}
	tracker.subscribe({
		version: 1,
		body: {
			debuggers: SUPPORTED_TYPES,
			wantCurrentStatus: true,
			notifyAllEvents: false,
			debugLevel: 0,
			handler: (event: TrackerEvent) => {
				void handleTrackerEvent(event, deps);
			}
		}
	});
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
	log: (message: string) => void;
}

async function handleTrackerEvent(event: TrackerEvent, deps: TrackerDeps): Promise<void> {
	const { sessions, panel, config, sessionFor } = deps;
	const session = event.session;
	if (!session) {
		return;
	}
	switch (event.event) {
		case 'terminated':
			sessions.get(session.id)?.discardLaunchFiles();
			panel.sessionEnded(session.id);
			sessions.delete(session.id);
			return;
		case 'started': {
			panel.sessionStarted(session.id, session.name);
			const gs = sessionFor(session);
			void gs.watchLaunchPython().then((ok) => {
				if (!sessions.has(session.id) || ok || gs.errorMessage === null) {
					return;
				}
				panel.sessionFailed(session.id, gs.errorMessage);
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
			const gs = sessionFor(session);
			if (!gs.initialized) {
				if (!sessionFactory) {
					return;
				}
				await sessionFactory(session, frameId);
				if (gs.initialized) {
					panel.sessionInitialized(session.id);
					if (config().get<boolean>('refreshOnStop', true)) {
						panel.firstStackTrace(session.id, frameId);
					}
				} else {
					panel.sessionFailed(session.id, gs.errorMessage ?? 'the session could not be initialized');
				}
				return;
			}
			panel.sessionInitialized(session.id);
			if (config().get<boolean>('refreshOnStop', true)) {
				panel.firstStackTrace(session.id, frameId);
			}
			return;
		}
	}
}

export function deactivate(): void {
	sessionFactory = undefined;
}
