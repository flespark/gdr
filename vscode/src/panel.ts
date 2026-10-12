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
import { isStale, parseHeapMap, parseSnapshot, type CostOptions, type HeapMapData, type PanelSnapshot } from './snapshot.ts';
import {
	MEMORY_VIEW_EXTENSION_ID,
	memoryViewMissingHint,
	memoryViewUri
} from './memoryNav.ts';
import { renderApp, renderShell, type ShellArgs } from './webview/render.ts';
import { decodeWebviewMessage, type PanelLayout } from './webview/messages.ts';
import { changedKeys } from './diff.ts';
import { NotificationGate, snapshotWarnings, type NotificationLevel } from './notifications.ts';
import { resolveSourceFile, type SourceResolution } from './sourceFiles.ts';
import type { PanelSessionState, SessionStatus } from './webview/panelState.ts';

/** How long a burst of stops must settle before one refresh runs. */
const REFRESH_DEBOUNCE_MS = 120;

/** The layout a panel starts with: every table and column shown, the default order. */
const DEFAULT_LAYOUT: PanelLayout = { order: [], hiddenTables: [], tables: {} };

/** Snapshot arguments for the current cost settings. */
export type CostOptionsGetter = () => CostOptions;

/** The one thing the panel asks the session layer for. */
export type SnapshotFetcher = (sessionId: string, frameId: number) => Promise<GdrEnvelope | null>;

/** The heap block layout for one session's current stop (the `heap-dump` verb). */
export type HeapMapFetcher = (sessionId: string, frameId: number) => Promise<GdrEnvelope | null>;

export interface PanelOptions {
	log: (message: string) => void;
	extensionUri: vscode.Uri;
	costOptions: CostOptionsGetter;
	/** Directory to keep one snapshot JSON per refresh; empty disables. */
	recordDir: () => string;
	fetchSnapshot: SnapshotFetcher;
	/** The heap block layout for a session's stop; the canvas draws it. */
	fetchHeapMap: HeapMapFetcher;
	/**
	 * The panel's saved layout, kept per workspace. Injected so the panel never
	 * touches the VS Code memento itself; the extension supplies the store.
	 */
	layoutStore: LayoutStore;
}

/** Read and write the panel layout. */
export interface LayoutStore {
	read(): PanelLayout | null;
	write(layout: PanelLayout): void;
}

interface SessionEntry {
	notifications: NotificationGate;
	state: PanelSessionState;
	lastGeneration: number | null;
	frameId?: number;
	refreshSeq: number;
	inFlight: boolean;
	pending: boolean;
	debounce: NodeJS.Timeout | null;
	/** Cells that changed at the last applied stop, keyed for the renderer. */
	changed: Set<string>;
	/** Whether the reader asked for the heap bitmap in this session. */
	heapRequested: boolean;
	/** The last heap map fetched for the session, drawn on its canvas. */
	heapMap: HeapMapData | null;
}

export class RtosViewsPanel implements vscode.WebviewViewProvider {
	public static readonly viewType = 'gdrRtosViews.panel';

	private readonly sessions = new Map<string, SessionEntry>();
	/** Sessions whose panel was already revealed once (discoverability). */
	private readonly revealed = new Set<string>();
	private activeId: string | null = null;
	/** The reader's table layout: order, shown tables and columns, widths. */
	private layout: PanelLayout;
	private view: vscode.WebviewView | undefined;

	constructor(private readonly options: PanelOptions) {
		this.layout = options.layoutStore.read() ?? DEFAULT_LAYOUT;
	}

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
			notifications: new NotificationGate(),
			state: { id: sessionId, name, status: 'waiting', message: null },
			lastGeneration: null,
			refreshSeq: 0,
			inFlight: false,
			pending: false,
			debounce: null,
			changed: new Set<string>(),
			heapRequested: false,
			heapMap: null
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
		entry.state.message = null;
		this.scheduleRefresh(sessionId);
		this.broadcast();
	}

	sessionFailed(sessionId: string, reason: string, level: NotificationLevel = 'error'): void {
		const entry = this.sessions.get(sessionId);
		if (entry === undefined) {
			return;
		}
		entry.state.status = 'error';
		entry.state.message = reason;
		this.notify(sessionId, level, `panel startup failed for "${entry.state.name}": ${reason}`);
		this.broadcast();
	}

	/** Errors remain in session status; popups report each distinct cause once. */
	private notify(sessionId: string | null | undefined, level: NotificationLevel, message: string): void {
		const gate = this.sessions.get(sessionId ?? this.activeId ?? '')?.notifications;
		if (gate !== undefined && !gate.accept(level, message)) { return; }
		this.options.log(`panel: ${level}: ${message}`);
		if (level === 'error') { void vscode.window.showErrorMessage(`GDR: ${message}`); }
		else { void vscode.window.showWarningMessage(`GDR: ${message}`); }
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
		} catch (error) {
			this.sessionFailed(sessionId, `snapshot request failed: ${String(error)}`);
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
			noDetails: this.options.costOptions().noDetails,
			data
		};
		const previous = entry.state.snapshot ?? null;
		entry.changed = changedKeys(previous, snapshot);
		entry.state.snapshot = snapshot;
		entry.state.status = 'ok';
		entry.state.message = null;
		for (const warning of snapshotWarnings(envelope)) { this.notify(sessionId, 'warning', warning); }
		this.record(sessionId, envelope);
		this.revealOnce(sessionId);
		this.broadcast();
		if (entry.heapRequested) {
			// Reason: the bitmap follows the stop the reader is looking at; a drawn
			// map from an earlier stop would name blocks that have since moved.
			void this.refreshHeapMap(sessionId, entry).catch((error) => this.notify(sessionId, 'error', `heap map refresh failed: ${String(error)}`));
		}
	}

	/** Re-fetch the heap map for a session whose bitmap the reader asked for. */
	private async refreshHeapMap(sessionId: string, entry: SessionEntry): Promise<void> {
		const frameId = entry.frameId;
		if (frameId === undefined) {
			return;
		}
		const envelope = await this.options.fetchHeapMap(sessionId, frameId);
		const map = envelope !== null && envelope.ok ? parseHeapMap(envelope.data) : null;
		if (map === null) {
			entry.heapMap = null;
			return;
		}
		entry.heapMap = map;
		void this.view?.webview.postMessage({ type: 'heapMap', sessionId, map });
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
		if (decoded === null) {
			return;
		}
		switch (decoded.type) {
			case 'selectSession':
				if (this.sessions.has(decoded.sessionId)) { this.activate(decoded.sessionId); }
				return;
			case 'openMemory':
				void this.openMemory(decoded.address, decoded.sessionId).catch((error) => this.notify(decoded.sessionId, 'error', String(error)));
				return;
			case 'openSource':
				void this.openSource(decoded.file, decoded.name, decoded.line, decoded.fallbackAddress, decoded.sessionId).catch((error) => this.notify(decoded.sessionId, 'error', String(error)));
				return;
			case 'refresh':
				this.refreshNow(decoded.sessionId);
				return;
			case 'heapDump':
				// Reason: the bitmap is requested on demand (plan D6); the request is
				// answered by the heap-map fetch wired in the canvas step.
				void this.heapDumpRequested(decoded.sessionId).catch((error) => this.notify(decoded.sessionId, 'error', `heap map request failed: ${String(error)}`));
				return;
			case 'layout':
				this.layoutChanged(decoded.layout);
				return;
		}
	}

	// -- source navigation -------------------------------------------------

	/** Open the file a task entry starts in, with the caret on its line. */
	private async openSource(
		file: string,
		name: string,
		line: number,
		fallbackAddress: string | undefined,
		sessionId: string | undefined
	): Promise<void> {
		const position = new vscode.Position(line - 1, 0);
		const resolved = await this.resolveSource(file, name);
		if (resolved.kind === 'found') {
			await this.showSource(resolved.path, position);
			return;
		}
		if (resolved.kind === 'ambiguous') {
			const picked = await vscode.window.showQuickPick(resolved.paths, {
				placeHolder: `Which ${name} does line ${line} belong to?`
			});
			if (picked !== undefined) {
				await this.showSource(picked, position);
			}
			return;
		}
		this.options.log(`panel: no workspace file for ${file}:${line} (${name})`);
		if (fallbackAddress !== undefined) {
			await this.openMemory(fallbackAddress, sessionId);
			return;
		}
		this.notify(sessionId, 'warning', `could not find ${name} in the workspace; check that the source is in this folder`);
	}

	/** Open one file with the caret on a line; a failure is logged, not thrown. */
	private async showSource(file: string, position: vscode.Position): Promise<void> {
		try {
			const document = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
			await vscode.window.showTextDocument(document, {
				selection: new vscode.Range(position, position)
			});
		} catch (error) {
			this.notify(this.activeId, 'error', `could not open ${file}: ${String(error)}`);
		}
	}

	/**
	 * Find the file a debug-info source path names: the path GDB resolved when it
	 * exists here, else the longest workspace match on the recorded name.
	 */
	private async resolveSource(file: string, name: string): Promise<SourceResolution> {
		const basename = path.basename(name.replace(/\\/g, '/'));
		const found = await vscode.workspace.findFiles(`**/${basename}`, '**/node_modules/**', 200);
		const candidates = found.map((uri) => uri.fsPath);
		return resolveSourceFile(name, file, candidates, (candidate) => fs.existsSync(candidate));
	}

	/** A heap-dump request from the heap card: fetch the block layout and draw it. */
	private async heapDumpRequested(sessionId: string | undefined): Promise<void> {
		const id = sessionId ?? this.activeId;
		if (id === null) {
			return;
		}
		const entry = this.sessions.get(id);
		const frameId = entry?.frameId;
		if (entry === undefined || frameId === undefined) {
			return;
		}
		entry.heapRequested = true;
		const envelope = await this.options.fetchHeapMap(id, frameId);
		const map = envelope !== null && envelope.ok ? parseHeapMap(envelope.data) : null;
		if (map === null) {
			this.options.log(`panel: no heap map for ${entry.state.name}: ${envelope?.error?.message ?? 'not drawable'}`);
			this.notify(id, 'warning', `no heap map to draw for ${entry.state.name}: ${envelope?.error?.message ?? 'allocator data is unavailable'}`);
			entry.heapRequested = false;
			this.broadcast();
			return;
		}
		entry.heapMap = map;
		this.broadcast();
		void this.view?.webview.postMessage({ type: 'heapMap', sessionId: id, map });
	}

	/**
	 * The reader changed the order, the shown tables or columns, or a width.
	 * The layout is saved for the workspace and the panel is redrawn with it.
	 */
	private layoutChanged(layout: PanelLayout): void {
		this.layout = layout;
		this.options.layoutStore.write(layout);
		this.broadcast();
	}

	// -- address navigation ------------------------------------------------

	private async openMemory(address: string, sessionId: string | undefined): Promise<void> {
		const extension = vscode.extensions.getExtension(MEMORY_VIEW_EXTENSION_ID);
		if (extension === undefined) {
			this.notify(sessionId, 'warning', `${memoryViewMissingHint()}. The address ${address} was copied to the clipboard instead.`);
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
			this.notify(sessionId, 'error', `could not open the memory view for ${address}`);
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
		void this.view.webview.postMessage({
			type: 'state',
			layout: this.layout,
			map: this.activeId === null ? null : this.sessions.get(this.activeId)?.heapMap ?? null,
			html: renderApp(state, { layout: this.layout, changed: this.changedFor(this.activeId), heapCanvas: this.heapRequestedFor(this.activeId) })
		});
	}

	/** Whether the active session has asked for its heap bitmap. */
	private heapRequestedFor(sessionId: string | null): boolean {
		if (sessionId === null) {
			return false;
		}
		return this.sessions.get(sessionId)?.heapRequested === true;
	}

	/** The cells that changed at the active session's last applied stop. */
	private changedFor(sessionId: string | null): Set<string> {
		if (sessionId === null) {
			return new Set<string>();
		}
		return this.sessions.get(sessionId)?.changed ?? new Set<string>();
	}
}
