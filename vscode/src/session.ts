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

import {
	consoleCommand,
	initCommand,
	launchLoaderPath,
	parsePythonResult,
	pythonMissingMessage,
	pythonResultPath,
	unsupportedReason,
	watchExpression
} from './inject.ts';
import { cacheKey, decideInit, parseProbePayload, type GdrSettings, type InitDecision } from './probe.ts';
import type { NotificationLevel } from './notifications.ts';
import { checkRuntime, locateRuntime, type RuntimeLocation } from './runtime.ts';
import {
	describeFailure,
	dumpExpression,
	evaluateExpression,
	chunkExpression,
	isArraySummary,
	parseDumpSize,
	parseEnvelope,
	reassembleChunks,
	transportPlan,
	type GdrEnvelope
} from './transport.ts';

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
	failureLevel: NotificationLevel;
}

export class GdrSession {
	private readonly state: SessionState = {
		python: null,
		initialized: null,
		decision: null,
		errorMessage: null,
		failureLevel: 'error'
	};

	private closed = false;
	private launchPythonReady: Promise<boolean> | null = null;
	/** Set once `evaluate` proved unable to carry a string (see `fetch`). */
	private fileRoute = false;

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

	/** Missing configuration/selection is a warning; runtime failures are errors. */
	get failureLevel(): NotificationLevel {
		return this.state.failureLevel;
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
		this.state.failureLevel = 'error';
		this.state.errorMessage = null;
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
			const problem = checkRuntime(this.runtime);
			if (problem !== null) {
				this.state.errorMessage = problem.message;
				this.log(problem.message);
				this.launchPythonReady = Promise.resolve(false);
				return this.launchPythonReady;
			}
			const loader = launchLoaderPath(this.session.configuration);
			if (loader === null) {
				// Reason: without the loader path no result file can ever be read, so
				// waiting would only hide the missing injection behind a 3-minute stall.
				this.state.errorMessage =
					'this launch configuration carries no GDR loader path, so GDR was not injected into it';
				this.log(this.state.errorMessage);
				this.launchPythonReady = Promise.resolve(false);
			} else {
				this.launchPythonReady = this.pollLaunchPython();
			}
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
			const message = pythonMissingMessage(this.session.type);
			this.state.errorMessage = message;
			this.log(message);
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
			if (this.state.errorMessage === null) {
				this.state.errorMessage = `${decision.reason}; configure gdr.rtos and gdr.version for this firmware`;
				this.state.failureLevel = 'warning';
			}
			this.log(this.state.errorMessage);
			return false;
		}
		let target: { rtos: string; version: string; source: 'settings' | 'probe' | 'cache' };
		if (decision.kind === 'init') {
			target = decision;
		} else {
			if (decision.options.length === 0) {
				this.state.errorMessage = `${decision.reason}; configure gdr.rtos and gdr.version for this firmware`;
				this.state.failureLevel = 'warning';
				this.log(this.state.errorMessage);
				return false;
			}
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
				this.state.errorMessage = 'RTOS selection was cancelled; select an adapter at the next stop or configure gdr.rtos and gdr.version';
				this.state.failureLevel = 'warning';
				this.log(this.state.errorMessage);
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
		await this.evaluate(consoleCommand(this.session.type, command), frameId, 'repl');
		const envelope = await this.fetch('capabilities', [], frameId);
		if (envelope === null || !envelope.ok) {
			const detail = envelope !== null
				? ` (${describeFailure(envelope)})`
				: ` (${this.state.errorMessage ?? 'no capabilities envelope came back'})`;
			this.state.errorMessage =
				`initialization of ${target.rtos} ${target.version} did not take effect${detail}` +
				"; GDR's reason is in the Debug Console" +
				(target.source === 'settings'
					? ''
					: ', and setting gdr.rtos/gdr.version forces registration with --allow-unverified');
			this.log(this.state.errorMessage);
			return false;
		}
		this.state.errorMessage = null;
		this.state.initialized = { rtos: target.rtos, version: target.version };
		this.cache.write(this.key, this.state.initialized);
		return true;
	}

	/** Ask GDR what this target is, through the one pre-init verb. */
	async probe(frameId: number): Promise<ReturnType<typeof parseProbePayload>> {
		const envelope = await this.fetch('probe', [], frameId);
		if (envelope === null || !envelope.ok) {
			this.state.errorMessage = envelope === null
				? `GDR did not answer the probe: ${this.state.errorMessage ?? 'no envelope came back'}; check GDR loading in the Debug Console`
				: `GDR probe failed: ${describeFailure(envelope)}`;
			this.log(this.state.errorMessage);
			return null;
		}
		const payload = parseProbePayload(envelope.data);
		this.state.errorMessage = payload === null ? 'GDR probe returned malformed adapter data' : null;
		return payload;
	}

	/**
	 * Fetch one verb's envelope.
	 *
	 * A large payload arrives as chunk frames from one frozen snapshot; they are
	 * reassembled and checksum-verified before anything is parsed. An adapter
	 * whose `evaluate` answers with the MI summary of the returned `char[N]`
	 * (`[405]`) cannot carry a string at all, so such a payload switches the
	 * session to GDR's file route for every later verb.
	 */
	async fetch(
		verb: string,
		args: string[],
		frameId: number,
		record?: { dir: string }
	): Promise<GdrEnvelope | null> {
		if (this.fileRoute) {
			return this.fetchViaFile(verb, args, frameId, record?.dir ?? '', false);
		}
		const first = await this.evaluate(evaluateExpression(verb, ...args), frameId, 'watch');
		if (first === null) {
			return null;
		}
		const plan = transportPlan(first);
		if (plan.kind === 'inline') {
			return parseEnvelope(plan.payload);
		}
		if (plan.kind === 'invalid') {
			if (isArraySummary(plan.payload)) {
				// Reason: cortex-debug answers `evaluate` through an MI varobj, whose
				// value for a `char[N]` is only the element count (`[405]`), so the
				// string never reaches the extension. GDR can write the same envelope
				// to a file instead -- that is a capability of the adapter, not a user
				// preference, which is why this switches on its own.
				this.log(
					`${verb}: evaluate cannot carry a string, ` +
						'reading GDR payloads from files instead'
				);
				this.fileRoute = true;
				return this.fetchViaFile(verb, args, frameId, record?.dir ?? '', false);
			}
			this.state.errorMessage = `GDR sent an unrecognized payload for ${verb}: ${plan.payload.slice(0, 120)}`;
			this.log(this.state.errorMessage);
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
	 * ``$gdr_dump(path, "snapshot")`` writes one envelope to a file
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
		return this.fetchViaFile('snapshot', args, frameId, recordDir, recordDir !== '');
	}

	/**
	 * Read one verb's envelope through a file GDB writes.
	 *
	 * ``$gdr_dump(path, verb)`` writes the envelope where GDB runs and answers
	 * with its byte count, so the payload never has to survive an adapter's
	 * value rendering (see `fetch`) and the caller learns from a scalar -- not
	 * from a missing file -- whether the write was accepted. When *keep* is
	 * false the file is removed after reading.
	 */
	private async fetchViaFile(
		verb: string,
		args: string[],
		frameId: number,
		recordDir: string,
		keep: boolean
	): Promise<GdrEnvelope | null> {
		const stamp = new Date().toISOString().replace(/[:.]/g, '-');
		const directory = recordDir !== '' ? recordDir : os.tmpdir();
		const file = path.join(directory, `gdr-${verb}-${stamp}-${this.session.id.slice(0, 8)}.json`);
		try {
			fs.mkdirSync(directory, { recursive: true });
			const answer = await this.evaluate(dumpExpression(file, verb, ...args), frameId, 'watch');
			const written = answer === null ? null : parseDumpSize(answer);
			if (written === null || written < 0) {
				this.state.errorMessage =
					written === null
						? `GDR did not answer $gdr_dump for ${verb}: ${answer ?? 'no result'}`
						: `GDR could not write the ${verb} file (${written})`;
				this.log(this.state.errorMessage);
				return null;
			}
			const envelope = parseEnvelope(fs.readFileSync(file, 'utf8'));
			if (envelope === null) {
				this.state.errorMessage = `the ${verb} file ${file} did not contain an envelope`;
				return null;
			}
			return envelope;
		} catch (error) {
			this.state.errorMessage = `the file bypass failed: ${String(error)}`;
			return null;
		} finally {
			if (!keep) {
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
	 * A `repl` context runs a GDB console command spelled by `consoleCommand`
	 * (its output belongs to the Debug Console); any other context returns the
	 * value, which is how GDR's `$gdr_json` payload reaches the plugin. A value
	 * expression is spelled by `watchExpression` first, because each adapter
	 * quotes or prefixes differently.
	 */
	private async evaluate(
		expression: string,
		frameId: number,
		context: 'repl' | 'watch'
	): Promise<string | null> {
		try {
			const response = await this.session.customRequest('evaluate', {
				expression: context === 'watch' ? watchExpression(this.session.type, expression) : expression,
				frameId,
				context
			});
			const typed = response as EvaluateResponse | undefined;
			if (context === 'repl') {
				return typed === undefined ? null : '';
			}
			if (typeof typed?.result === 'string') { return typed.result; }
			this.state.errorMessage = `the debug adapter returned no value for ${expression}`;
			return null;
		} catch (error) {
			this.state.errorMessage = `${expression}: ${error instanceof Error ? error.message : String(error)}`;
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
