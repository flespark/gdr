/**
 * One debug session's GDR lifecycle.
 *
 * The plugin works only after the session starts: at the first stop it loads
 * the runtime, confirms the session's GDB has Python, asks GDR which adapter
 * this target is, initializes it, and only then has data to show. Every step
 * reports a precise reason on failure instead of leaving an empty panel.
 *
 * All target access happens inside the debugger's own GDB through DAP; this
 * module never reads or writes target memory itself.
 */

import * as vscode from 'vscode';

import { injectionPlan, initCommand } from './inject';
import { cacheKey, decideInit, parseProbePayload, type GdrSettings, type InitDecision } from './probe';
import { checkRuntime, locateRuntime, type RuntimeLocation } from './runtime';
import {
	describeFailure,
	evaluateExpression,
	chunkExpression,
	parseEnvelope,
	reassembleChunks,
	transportPlan,
	type GdrEnvelope
} from './transport';

/** The subset of a DAP evaluate response this client reads. */
interface EvaluateResponse {
	result?: string;
	variablesReference?: number;
}

export interface SessionState {
	/** Whether the source/init commands have been issued for this session. */
	loaded: boolean;
	/** Whether the session's GDB has the Python interpreter GDR needs. */
	python: boolean | null;
	/** Adapter and version currently initialized, once known. */
	initialized: { rtos: string; version: string } | null;
	/** The detection decision last taken, for reporting. */
	decision: InitDecision | null;
}

export class GdrSession {
	private readonly state: SessionState = {
		loaded: false,
		python: null,
		initialized: null,
		decision: null
	};

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

	/**
	 * Run the lifecycle up to the point where data can be read.
	 *
	 * Returns `true` when the session is initialized and verbs will answer.
	 */
	async onFirstStop(frameId: number): Promise<boolean> {
		const request = this.session.configuration.request === 'attach' ? 'attach' : 'launch';
		const plan = injectionPlan(this.session.type, request, {
			runtimePath: this.runtime.directory,
			init: null
		});
		if (!plan.supported) {
			this.log(plan.reason ?? 'this debug adapter cannot host GDR');
			return false;
		}
		const problem = checkRuntime(this.runtime);
		if (problem !== null) {
			this.log(problem.message);
			return false;
		}
		if (!(await this.ensurePython(frameId))) {
			return false;
		}
		await this.ensureLoaded(frameId, plan.replCommands);
		const decision = decideInit(await this.probe(frameId), this.settings, this.cache.read(this.key));
		this.state.decision = decision;
		return this.applyDecision(decision, frameId);
	}

	/**
	 * GDR needs GDB's embedded Python; a GDB built without it cannot host the
	 * helper at all, and the message names the fix rather than the symptom.
	 */
	private async ensurePython(frameId: number): Promise<boolean> {
		if (this.state.python !== null) {
			return this.state.python;
		}
		const response = await this.evaluate('python print("gdr-python-ok")', frameId, 'repl');
		this.state.python = response !== null;
		if (!response) {
			this.log(
				'this GDB has no Python interpreter, so GDR cannot run in it. xPack ships ' +
					'both variants: `arm-none-eabi-gdb` has no Python, `arm-none-eabi-gdb-py3` does. ' +
					'Point the launch configuration (or the adapter\'s gdbPath setting) at the -py3 binary.'
			);
		}
		return this.state.python;
	}

	/** Load GDR, unless the launch configuration already did it. */
	private async ensureLoaded(frameId: number, replCommands: string[]): Promise<void> {
		if (this.state.loaded) {
			return;
		}
		for (const command of replCommands) {
			await this.evaluate(command, frameId, 'repl');
		}
		this.state.loaded = true;
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
				source: 'settings'
			};
		}
		this.log(`initializing ${target.rtos} ${target.version} (${target.source})`);
		await this.evaluate(initCommand({ rtos: target.rtos, version: target.version }), frameId, 'repl');
		const envelope = await this.fetch('capabilities', [], frameId);
		if (envelope === null || !envelope.ok) {
			this.log(
				`initialization of ${target.rtos} ${target.version} did not take effect` +
					(envelope !== null ? ` (${describeFailure(envelope)})` : '') +
					'; GDR\'s warning in the Debug Console names the reason'
			);
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
