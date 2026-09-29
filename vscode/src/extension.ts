/**
 * Extension entry point: wire debug sessions to their GDR helper.
 *
 * Responsibilities kept here (and not in `session.ts`): reading settings,
 * loading the runtime path, injecting the loader through each adapter's launch
 * configuration, and following the debug tracker's stop events so a session's
 * lifecycle starts at the first stop -- GDR is never driven while the target
 * runs.
 */

import * as vscode from 'vscode';

import { injectionPlan } from './inject';
import { GdrCache, GdrSession, runtimeFor } from './session';
import type { GdrSettings } from './probe';

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

interface TrackerEvent {
	event?: string;
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

	const settings = (): GdrSettings => {
		const config = vscode.workspace.getConfiguration('gdr');
		return {
			rtos: config.get<string>('rtos', ''),
			version: config.get<string>('version', ''),
			autoInit: config.get<boolean>('autoInit', true)
		};
	};

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

	// Load GDR during launch when the adapter has a hook for it, so the helper
	// exists before the first `p` or watch expression a user types.
	context.subscriptions.push(
		vscode.debug.registerDebugConfigurationProvider('*', {
			resolveDebugConfiguration: (folder, config) => {
				if (!SUPPORTED_TYPES.includes(config.type)) {
					return config;
				}
				const runtime = runtimeFor(context.extensionUri, vscode.workspace.getConfiguration('gdr').get<string>('runtimePath', ''));
				const plan = injectionPlan(config.type, config.request === 'attach' ? 'attach' : 'launch', {
					runtimePath: runtime.directory,
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
		})
	);

	void subscribeToTracker(context, log, sessions);
}

/**
 * Follow stop events through `debug-tracker-vscode`.
 *
 * The tracker is a declared extension dependency; a client that cannot reach it
 * says so once instead of silently never starting.
 */
async function subscribeToTracker(
	context: vscode.ExtensionContext,
	log: (message: string) => void,
	sessions: Map<string, GdrSession>
): Promise<void> {
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
				void handleTrackerEvent(event, sessions, log);
			}
		}
	});
	void context;
}

async function handleTrackerEvent(
	event: TrackerEvent,
	sessions: Map<string, GdrSession>,
	log: (message: string) => void
): Promise<void> {
	const session = event.session;
	if (!session) {
		return;
	}
	switch (event.event) {
		case 'Terminated':
			sessions.delete(session.id);
			return;
		case 'FirstStackTrace': {
			const frameId = event.stackTrace?.body?.stackFrames?.[0]?.id;
			if (typeof frameId !== 'number') {
				return;
			}
			if (sessionFactory) {
				await sessionFactory(session, frameId);
			}
			return;
		}
		default:
			log(`debug event ${event.event ?? 'unknown'}`);
	}
}

export function deactivate(): void {
	sessionFactory = undefined;
}
