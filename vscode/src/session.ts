/**
 * One debug session's GDR lifecycle.
 *
 * Launch checks Python and loads GDR. A missing interpreter is recorded for
 * the panel and does not abort the session. At the first stop this module
 * asks GDR which adapter this target is, initializes it, and only then has
 * data to show. It does not check Python or source GDR again. Every step
 * reports a precise reason on failure instead of leaving an empty panel.
 *
 * All target access happens inside the debugger's own GDB through DAP; this
 * module never reads or writes target memory itself.
 */

/// <reference types="node" />
// Reason: this subproject keeps its own node_modules, so an editor or `tsc`
// invocation whose analysis root is the repository root would otherwise not
// resolve the Node builtins this file uses.

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import * as vscode from 'vscode';

import { initCommand, launchLoaderPath, parsePythonResult, pythonResultPath, unsupportedReason } from './inject.ts';
import { cacheKey, decideInit, parseProbePayload, type GdrSettings, type InitDecision } from './probe.ts';
import { checkRuntime, locateRuntime, type RuntimeLocation } from './runtime.ts';
import {
	describeFailure,
	evaluateExpression,
	chunkExpression,
	parseEnvelope,
	reassembleChunks,
	transportPlan,
	type GdrEnvelope
} from './transport.ts';

/**
 * Shown in the panel when launch finds a GDB built without Python.
 *
 * The launch script only records the result; this is the text the user sees.
 */
export const PYTHON_MISSING_MESSAGE =
	'This GDB has no Python interpreter, so GDR cannot run in it. ' +
	'xPack ships both variants: arm-none-eabi-gdb has no Python, arm-none-eabi-gdb-py3 does. ' +
	"Point the launch configuration's gdbPath at the -py3 binary.";

/** The subset of a DAP evaluate response this client reads. */
interface EvaluateResponse {
	result?: string;
	variablesReference?: number;
}

export interface SessionState {
	/** Whether the session's GDB has the Python interpreter GDR needs. */
	python: boolean | null;
	/** Adapter and version currently initialized, once known. */
	initialized: { rtos: string; version: string } | null;
	/** The detection decision last taken, for reporting. */
	decision: InitDecision | null;
	/** The first problem that stopped the lifecycle, for the panel. */
	errorMessage: string | null;
}

export class GdrSession {
	private readonly state: SessionState = {
		python: null,
		initialized: null,
		decision: null,
		errorMessage: null
	};

	private closed = false;
	private launchPythonReady: Promise<boolean> | null = null;

	constructor(
		private readonly session: vscode.DebugSession,
		private readonly runtime: RuntimeLocation,
		private readonly settings: GdrSettings,
		private readonly log: (message: string) => void,
		private readonly cache: GdrCache
	) {}

	/** The decision this session is acting on, for the status report. */
	get decision(): InitDecision | null {
		return this.state.decision;
	}

	/** The adapter this session initialized, if any. */
	get initialized(): { rtos: string; version: string } | null {
		return this.state.initialized;
	}

	/** The first lifecycle failure, for the panel's status block. */
	get errorMessage(): string | null {
		return this.state.errorMessage;
	}

	/**
	 * Run the lifecycle up to the point where data can be read.
	 *
	 * Returns `true` when the session is initialized and verbs will answer.
	 */
	async onFirstStop(frameId: number): Promise<boolean> {
		const reason = unsupportedReason(this.session.type);
		if (reason !== null) {
			this.state.errorMessage = reason;
			this.log(this.state.errorMessage);
			return false;
		}
		const problem = checkRuntime(this.runtime);
		if (problem !== null) {
			this.state.errorMessage = problem.message;
			this.log(this.state.errorMessage);
			return false;
		}
		if (!(await this.watchLaunchPython())) {
			return false;
		}
		const decision = decideInit(await this.probe(frameId), this.settings, this.cache.read(this.key));
		this.state.decision = decision;
		return this.applyDecision(decision, frameId);
	}

	/**
	 * Wait until the launch-time Python check has recorded a result.
	 *
	 * `started` fires before the adapter runs the injected commands, so the
	 * result file appears during launch. The same promise serves the panel
	 * and the first stop.
	 */
	watchLaunchPython(): Promise<boolean> {
		if (this.launchPythonReady === null) {
			this.launchPythonReady = this.pollLaunchPython();
		}
		return this.launchPythonReady;
	}

	/**
	 * Apply the result file the launch-time Python check wrote.
	 *
	 * Returns `true` once that result is known. A missing file means the
	 * launch commands have not finished yet.
	 */
	private consumeLaunchPython(): boolean {
		if (this.state.python !== null) {
			return true;
		}
		const loader = launchLoaderPath(this.session.configuration);
		if (loader === null) {
			return false;
		}
		let text: string;
		try {
			text = fs.readFileSync(pythonResultPath(loader), 'utf8');
		} catch {
			return false;
		}
		const result = parsePythonResult(text);
		if (result === null) {
			return false;
		}
		this.state.python = result === 'present';
		if (!this.state.python) {
			this.state.errorMessage = PYTHON_MISSING_MESSAGE;
			this.log(PYTHON_MISSING_MESSAGE);
		}
		return true;
	}

	private async pollLaunchPython(): Promise<boolean> {
		const deadline = Date.now() + 180_000;
		while (Date.now() < deadline && !this.closed) {
			if (this.consumeLaunchPython()) {
				return this.state.python === true;
			}
			await new Promise((resolve) => setTimeout(resolve, 200));
		}
		if (!this.closed && this.state.errorMessage === null) {
			this.state.errorMessage = 'GDR did not record a Python check at launch';
			this.log(this.state.errorMessage);
		}
		return false;
	}

	/** Remove the launch-time loader and result once the session is gone. */
	discardLaunchFiles(): void {
		this.closed = true;
		const loader = launchLoaderPath(this.session.configuration);
		if (loader === null) {
			return;
		}
		for (const file of [loader, pythonResultPath(loader)]) {
			try {
				fs.rmSync(file, { force: true });
			} catch {
				// A leftover temp file is minor next to a session that already ended.
			}
		}
	}

	private async applyDecision(decision: InitDecision, frameId: number): Promise<boolean> {
		if (decision.kind === 'none') {
			this.log(`no adapter to initialize: ${decision.reason}`);
			return false;
		}
		let target: { rtos: string; version: string; source: 'settings' | 'probe' | 'cache' };
		if (decision.kind === 'init') {
			target = decision;
		} else {
			const picked = await vscode.window.showQuickPick(
				decision.options.map((option) => ({
					label: `${option.rtos} ${option.version}`,
					description: option.rtos,
					detail: decision.evidence.join('\n'),
					option
				})),
				{ title: `GDR: ${decision.reason}`, placeHolder: 'Pick the RTOS and version to initialize' }
			);
			if (!picked) {
				return false;
			}
			target = {
				rtos: picked.option.rtos,
				version: picked.option.version,
				// Reason: the options came from the probe's evidence, so this is
				// a confirmed probe answer, not a manual assertion. The
				// distinction decides whether GDR's --allow-unverified escape
				// hatch is passed below.
				source: 'probe'
			};
		}
		this.log(`initializing ${target.rtos} ${target.version} (${target.source})`);
		// Reason: a manual gdr.rtos/gdr.version is the user's own assertion about
		// a target that may show no RTOS symbol at all (a modified or stripped
		// kernel), so it carries GDR's --allow-unverified. A probe answer does
		// not: GDR should refuse it if the target disagrees.
		const command = initCommand(
			{ rtos: target.rtos, version: target.version },
			{ allowUnverified: target.source === 'settings' }
		);
		await this.evaluate(command, frameId, 'repl');
		const envelope = await this.fetch('capabilities', [], frameId);
		if (envelope === null || !envelope.ok) {
			const detail = envelope !== null ? ` (${describeFailure(envelope)})` : '';
			this.state.errorMessage =
				`initialization of ${target.rtos} ${target.version} did not take effect${detail}` +
				"; GDR's reason is in the Debug Console" +
				(target.source === 'settings'
					? ''
					: ', and setting gdr.rtos/gdr.version forces registration with --allow-unverified');
			this.log(this.state.errorMessage);
			return false;
		}
		this.state.initialized = { rtos: target.rtos, version: target.version };
		this.cache.write(this.key, this.state.initialized);
		return true;
	}

	/** Ask GDR what this target is, through the one pre-init verb. */
	async probe(frameId: number): Promise<ReturnType<typeof parseProbePayload>> {
		const envelope = await this.fetch('probe', [], frameId);
		if (envelope === null || !envelope.ok) {
			this.log(
				envelope === null
					? 'GDR did not answer the probe (no envelope came back)'
					: `GDR probe failed: ${describeFailure(envelope)}`
			);
			return null;
		}
		return parseProbePayload(envelope.data);
	}

	/**
	 * Fetch one verb's envelope.
	 *
	 * A large payload arrives as chunk frames from one frozen snapshot; they are
	 * reassembled and checksum-verified before anything is parsed.
	 */
	async fetch(verb: string, args: string[], frameId: number): Promise<GdrEnvelope | null> {
		const first = await this.evaluate(evaluateExpression(verb, ...args), frameId, 'watch');
		if (first === null) {
			return null;
		}
		const plan = transportPlan(first);
		if (plan.kind === 'inline') {
			return parseEnvelope(plan.payload);
		}
		if (plan.kind === 'invalid') {
			this.log(`GDR sent an unrecognized payload for ${verb}: ${plan.payload.slice(0, 120)}`);
			return null;
		}
		const slices: string[] = [];
		for (let index = 1; index <= plan.meta.total; index += 1) {
			const frame = await this.evaluate(chunkExpression(index, verb, ...args), frameId, 'watch');
			if (frame === null) {
				return null;
			}
			slices.push(frame);
		}
		try {
			return parseEnvelope(reassembleChunks(plan.meta, slices));
		} catch (error) {
			this.log(String(error));
			return null;
		}
	}

	/**
	 * Fetch a snapshot through GDR's file bypass.
	 *
	 * ``gdr api snapshot --json --out <path>`` writes one envelope to a file
	 * where GDB runs; the chunked evaluate transport is then unnecessary for
	 * very large sessions. The condition is not where gdbserver runs but
	 * whether the extension reads the same filesystem as GDB: true for a
	 * locally started GDB (the usual embedded setup, local or remote probe
	 * server alike) and for Remote-SSH/Dev Containers, where the extension
	 * host runs beside GDB; false for a GDB in a separate namespace (a WSL or
	 * container GDB whose temp directory the extension cannot see). When
	 * *recordDir* is set the file doubles as the record and stays; otherwise
	 * it is removed after reading.
	 */
	async fetchSnapshotFile(
		frameId: number,
		args: string[],
		recordDir: string
	): Promise<GdrEnvelope | null> {
		const stamp = new Date().toISOString().replace(/[:.]/g, '-');
		const directory = recordDir !== '' ? recordDir : os.tmpdir();
		const file = path.join(directory, `gdr-snapshot-${stamp}-${this.session.id.slice(0, 8)}.json`);
		try {
			fs.mkdirSync(directory, { recursive: true });
			await this.evaluate(`gdr api snapshot ${args.join(' ')} --json --out ${quotePath(file)}`, frameId, 'repl');
			const envelope = parseEnvelope(fs.readFileSync(file, 'utf8'));
			if (envelope === null) {
				this.state.errorMessage = `the snapshot file ${file} did not contain an envelope`;
				return null;
			}
			return envelope;
		} catch (error) {
			this.state.errorMessage = `the file bypass failed: ${String(error)}`;
			return null;
		} finally {
			if (recordDir === '') {
				try {
					fs.rmSync(file, { force: true });
				} catch {
					// The record was already written or the read failed; a stray
					// temp file is a minor cost next to losing the answer.
				}
			}
		}
	}

	/**
	 * Evaluate one expression in the session.
	 *
	 * A `repl` context runs a GDB console command (its output belongs to the
	 * Debug Console); any other context returns the value, which is how GDR's
	 * `$gdr_json` payload reaches the plugin.
	 */
	private async evaluate(
		expression: string,
		frameId: number,
		context: 'repl' | 'watch'
	): Promise<string | null> {
		try {
			const response = await this.session.customRequest('evaluate', {
				expression,
				frameId,
				context
			});
			const typed = response as EvaluateResponse | undefined;
			if (context === 'repl') {
				return typed === undefined ? null : '';
			}
			return typeof typed?.result === 'string' ? typed.result : null;
		} catch (error) {
			if (context === 'repl') {
				// A failing console command still means the session is usable;
				// the caller verifies the effect through a data verb.
				this.log(`${expression}: ${String(error)}`);
				return null;
			}
			return null;
		}
	}

	private get key(): string | null {
		// ``DebugConfiguration`` carries an index signature, so it already
		// satisfies the launch-identity shape the cache key reads.
		return cacheKey(this.session.configuration);
	}
}

/** Quote one absolute path for a GDB console command. */
function quotePath(filePath: string): string {
	return `"${filePath.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
}

/** Session-scoped answers, persisted per launch configuration. */
export class GdrCache {
	constructor(private readonly store: vscode.Memento) {}

	read(key: string | null): { rtos: string; version: string } | null {
		if (key === null) {
			return null;
		}
		const value = this.store.get<{ rtos: string; version: string }>(`gdr.session.${key}`);
		return value ?? null;
	}

	write(key: string | null, value: { rtos: string; version: string }): void {
		if (key !== null) {
			void this.store.update(`gdr.session.${key}`, value);
		}
	}
}

/** Resolve the runtime for one session from settings plus the bundled copy. */
export function runtimeFor(extensionUri: vscode.Uri, configuredPath: string): RuntimeLocation {
	return locateRuntime(vscode.Uri.joinPath(extensionUri, 'resources', 'gdr').fsPath, configuredPath);
}
