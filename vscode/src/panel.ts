/**
 * The RTOS panel: one webview view hosting the sessions' snapshots.
 *
 * This controller owns the panel's view state (one tab per debug session),
 * the stop-driven refresh pipeline, the freshness guard that drops responses
 * that raced a newer stop, the optional JSON record for bug reports, and the
 * click-through to the ecosystem memory viewer. Every target read happens
 * through a caller-provided fetcher; the panel itself never touches a DAP
 * session.
 *
 * The webview is dumb: it receives host-rendered HTML (see `webview/render.ts`)
 * and posts clicks back. No envelope data is interpreted here beyond parsing
 * the snapshot the fetcher already validated.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';

import * as vscode from 'vscode';

import { describeFailure, type GdrEnvelope } from './transport.ts';
import { isStale, parseSnapshot, type CostOptions, type PanelSnapshot } from './snapshot.ts';
import {
	MEMORY_VIEW_EXTENSION_ID,
	memoryViewMissingHint,
	memoryViewUri
} from './memoryNav.ts';
import { renderApp, renderShell, type ShellArgs } from './webview/render.ts';
import type { PanelSessionState, SessionStatus } from './webview/panelState.ts';

/** How long a burst of stops must settle before one refresh runs. */
const REFRESH_DEBOUNCE_MS = 120;

/** Snapshot arguments for the current cost settings. */
export type CostOptionsGetter = () => CostOptions;

/** The one thing the panel asks the session layer for. */
export type SnapshotFetcher = (sessionId: string, frameId: number) => Promise<GdrEnvelope | null>;

export interface PanelOptions {
	log: (message: string) => void;
	extensionUri: vscode.Uri;
	costOptions: CostOptionsGetter;
	/** Directory to keep one snapshot JSON per refresh; empty disables. */
	recordDir: () => string;
	fetchSnapshot: SnapshotFetcher;
}

interface SessionEntry {
	state: PanelSessionState;
	lastGeneration: number | null;
	frameId?: number;
	refreshSeq: number;
	inFlight: boolean;
	pending: boolean;
	debounce: NodeJS.Timeout | null;
}

/** A message the webview can send, decoded at the I/O boundary. */
type WebviewMessage = { type: 'openMemory'; address: string; sessionId?: string };

function decodeWebviewMessage(value: unknown): WebviewMessage | null {
	if (typeof value !== 'object' || value === null) {
		return null;
	}
	const payload = value as Record<string, unknown>;
	if (payload.type !== 'openMemory' || typeof payload.address !== 'string') {
		return null;
	}
	const sessionId = typeof payload.sessionId === 'string' ? payload.sessionId : undefined;
	return { type: 'openMemory', address: payload.address, ...(sessionId !== undefined ? { sessionId } : {}) };
}

export class RtosViewsPanel implements vscode.WebviewViewProvider {
	public static readonly viewType = 'gdrRtosViews.panel';

	private readonly sessions = new Map<string, SessionEntry>();
	/** Sessions whose panel was already revealed once (discoverability). */
	private readonly revealed = new Set<string>();
	private activeId: string | null = null;
	private view: vscode.WebviewView | undefined;

	constructor(private readonly options: PanelOptions) {}

	// -- provider ----------------------------------------------------------

	resolveWebviewView(webviewView: vscode.WebviewView): void {
		this.view = webviewView;
		const webview = webviewView.webview;
		webview.options = {
			// Reason: the panel keeps its rendered markup while the debug
			// session steals focus; a rebuild on every switch would re-inject
			// the same HTML for no gain.
			enableScripts: true,
			// The compiled client lives under dist/, the stylesheet under
			// src/webview/; both must be servable to the webview.
			localResourceRoots: [
				vscode.Uri.joinPath(this.options.extensionUri, 'dist'),
				vscode.Uri.joinPath(this.options.extensionUri, 'src', 'webview')
			]
		};
		const shell: ShellArgs = {
			cspSource: webview.cspSource,
			styleUri: webview.asWebviewUri(
				vscode.Uri.joinPath(this.options.extensionUri, 'src', 'webview', 'style.css')
			).toString(),
			clientUri: webview.asWebviewUri(
				vscode.Uri.joinPath(this.options.extensionUri, 'dist', 'webview', 'client.js')
			).toString()
		};
		webview.html = renderShell(shell);
		webview.onDidReceiveMessage((message: unknown) => {
			this.handleMessage(message);
		});
		webviewView.onDidDispose(() => {
			if (this.view === webviewView) {
				this.view = undefined;
			}
		});
		this.broadcast();
	}

	// -- session lifecycle (called from the tracker wiring) ----------------

	/** A debug session appeared; it gets a tab before it is initialized. */
	sessionStarted(sessionId: string, name: string): void {
		if (this.sessions.has(sessionId)) {
			return;
		}
		const entry: SessionEntry = {
			state: { id: sessionId, name, status: 'waiting', message: null },
			lastGeneration: null,
			refreshSeq: 0,
			inFlight: false,
			pending: false,
			debounce: null
		};
		this.sessions.set(sessionId, entry);
		this.activate(sessionId);
		if (this.sessions.size === 1) {
			this.setVisible(true);
		}
		this.options.log(`panel: tracking ${name}`);
		this.broadcast();
	}

	/** The session's GDR adapter is live; the panel asks for data. */
	sessionInitialized(sessionId: string): void {
		const entry = this.sessions.get(sessionId);
		if (entry === undefined) {
			return;
		}
		entry.state.status = 'ok';
		this.scheduleRefresh(sessionId);
		this.broadcast();
	}

	sessionFailed(sessionId: string, reason: string): void {
		const entry = this.sessions.get(sessionId);
		if (entry === undefined) {
			return;
		}
		entry.state.status = 'error';
		entry.state.message = reason;
		this.broadcast();
	}

	/** The target resumed; the last snapshot stays, marked stale. */
	targetRunning(sessionId: string): void {
		const entry = this.sessions.get(sessionId);
		if (entry === undefined) {
			return;
		}
		entry.state.status = 'running';
		this.broadcast();
	}

	/** A fresh stop delivered a frame id; debounce one refresh per burst. */
	firstStackTrace(sessionId: string, frameId: number): void {
		const entry = this.sessions.get(sessionId);
		if (entry === undefined) {
			return;
		}
		entry.frameId = frameId;
		this.scheduleRefresh(sessionId);
	}

	/** The debug session ended; its tab goes away and the active one moves. */
	sessionEnded(sessionId: string): void {
		const entry = this.sessions.get(sessionId);
		if (entry !== null && entry !== undefined && entry.debounce !== null) {
			clearTimeout(entry.debounce);
		}
		this.sessions.delete(sessionId);
		this.revealed.delete(sessionId);
		if (this.sessions.size === 0) {
			this.setVisible(false);
		}
		if (this.activeId === sessionId) {
			this.activeId = this.sessions.size > 0 ? [...this.sessions.keys()][0] : null;
		}
		this.broadcast();
	}

	/** Refresh a session immediately (the panel's refresh button). */
	refreshNow(sessionId: string | undefined): void {
		const id = sessionId ?? this.activeId;
		if (id === null) {
			return;
		}
		void this.refresh(id);
	}

	// -- refresh pipeline --------------------------------------------------

	private scheduleRefresh(sessionId: string): void {
		const entry = this.sessions.get(sessionId);
		if (entry === undefined) {
			return;
		}
		if (entry.debounce !== null) {
			clearTimeout(entry.debounce);
		}
		entry.debounce = setTimeout(() => {
			entry.debounce = null;
			void this.refresh(sessionId);
		}, REFRESH_DEBOUNCE_MS);
	}

	private async refresh(sessionId: string): Promise<void> {
		const entry = this.sessions.get(sessionId);
		const frameId = entry?.frameId;
		if (entry === undefined || frameId === undefined) {
			return;
		}
		if (entry.inFlight) {
			entry.pending = true;
			return;
		}
		entry.inFlight = true;
		entry.refreshSeq += 1;
		const seq = entry.refreshSeq;
		this.options.log(`panel: refresh ${entry.state.name} (generation ${entry.lastGeneration ?? '-'})`);
		try {
			const envelope = await this.options.fetchSnapshot(sessionId, frameId);
			if (envelope === null) {
				this.sessionFailed(sessionId, 'the session did not answer the snapshot request');
				return;
			}
			if (seq !== entry.refreshSeq || isStale(entry.lastGeneration, envelope)) {
				this.options.log(`panel: dropped a stale snapshot for ${entry.state.name}`);
				return;
			}
			this.applyEnvelope(sessionId, entry, envelope);
		} finally {
			entry.inFlight = false;
			if (entry.pending) {
				entry.pending = false;
				void this.refresh(sessionId);
			}
		}
	}

	/** Validate, parse and publish one fresh envelope for a session. */
	private applyEnvelope(sessionId: string, entry: SessionEntry, envelope: GdrEnvelope): void {
		if (!envelope.ok) {
			this.sessionFailed(sessionId, describeFailure(envelope));
			return;
		}
		const data = parseSnapshot(envelope.data);
		if (data === null) {
			this.sessionFailed(sessionId, 'the snapshot payload did not parse as a GDR snapshot');
			return;
		}
		entry.lastGeneration = envelope.generation ?? null;
		const snapshot: PanelSnapshot = {
			rtos: envelope.rtos ?? null,
			rtosVersion: envelope.rtos_version ?? null,
			gdrVersion: envelope.gdr_version ?? null,
			generation: envelope.generation ?? null,
			elapsedMs: envelope.elapsed_ms ?? null,
			collectedAt: Date.now(),
			warnings: envelope.warnings ?? [],
			noHeap: this.options.costOptions().noHeap,
			data
		};
		entry.state.snapshot = snapshot;
		entry.state.status = 'ok';
		entry.state.message = null;
		this.record(sessionId, envelope);
		this.revealOnce(sessionId);
		this.broadcast();
	}

	/**
	 * Reveal the panel at a session's first snapshot, once.
	 *
	 * The view is gated on `gdrRtosViews.active` instead of a standing
	 * activity-bar icon, so the first data is also the moment to show where it
	 * lives; a per-session flag keeps a step-heavy session from re-focusing on
	 * every stop.
	 */
	private revealOnce(sessionId: string): void {
		if (this.revealed.has(sessionId)) {
			return;
		}
		this.revealed.add(sessionId);
		void vscode.commands.executeCommand('gdrRtosViews.panel.focus');
	}

	/** Flip the context key the view's `when` clause reads. */
	private setVisible(visible: boolean): void {
		void vscode.commands.executeCommand('setContext', 'gdrRtosViews.active', visible);
	}

	// -- record / replay ---------------------------------------------------

	private record(sessionId: string, envelope: GdrEnvelope): void {
		const directory = this.options.recordDir();
		if (directory === '') {
			return;
		}
		try {
			fs.mkdirSync(directory, { recursive: true });
			const stamp = new Date().toISOString().replace(/[:.]/g, '-');
			const file = path.join(directory, `gdr-snapshot-${stamp}-${sessionId.slice(0, 8)}.json`);
			// Reason: write next to the target and rename, so a crash mid-write
			// never leaves a half-written record that would parse as garbage.
			const temp = `${file}.tmp`;
			fs.writeFileSync(temp, JSON.stringify(envelope, null, 1), 'utf8');
			fs.renameSync(temp, file);
			this.options.log(`panel: recorded snapshot to ${file}`);
		} catch (error) {
			this.options.log(`panel: could not record a snapshot: ${String(error)}`);
		}
	}

	// -- messages from the webview ----------------------------------------

	private handleMessage(message: unknown): void {
		const decoded = decodeWebviewMessage(message);
		if (decoded !== null) {
			void this.openMemory(decoded.address, decoded.sessionId);
		}
	}

	// -- address navigation ------------------------------------------------

	private async openMemory(address: string, sessionId: string | undefined): Promise<void> {
		const extension = vscode.extensions.getExtension(MEMORY_VIEW_EXTENSION_ID);
		if (extension === undefined) {
			void vscode.window.showInformationMessage(
				`${memoryViewMissingHint()}. The address ${address} was copied to the clipboard instead.`
			);
			void vscode.env.clipboard.writeText(address);
			return;
		}
		const options: { sessionId?: string; displayName: string } = {
			displayName: `0x${address.replace(/^0x/i, '')}`
		};
		if (sessionId !== undefined) {
			options.sessionId = sessionId;
		}
		const uri = memoryViewUri(vscode.env.uriScheme, address, options);
		// Reason: the memory viewer registers a URI handler that evaluates the
		// bare address literal in the (stopped) session it was started from --
		// an address constant, never an inferior call.
		const opened = await vscode.env.openExternal(vscode.Uri.parse(uri));
		if (!opened) {
			void vscode.window.showInformationMessage(`GDR: could not open the memory view for ${address}`);
		}
	}

	// -- broadcast ---------------------------------------------------------

	private activate(sessionId: string): void {
		this.activeId = sessionId;
	}

	private broadcast(): void {
		if (this.view === undefined) {
			return;
		}
		const state = {
			sessions: [...this.sessions.values()].map((entry) => ({
				id: entry.state.id,
				name: entry.state.name,
				status: entry.state.status as SessionStatus,
				message: entry.state.message,
				...(entry.state.snapshot !== undefined ? { snapshot: entry.state.snapshot } : {})
			})),
			activeId: this.activeId
		};
		void this.view.webview.postMessage({ type: 'state', html: renderApp(state) });
	}
}
