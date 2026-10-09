/**
 * Closed-loop check of the plugin's data path through the STM32Cube ST-LINK GDB
 * Server adapter (`stlinkgdbtarget`).
 *
 * ## What runs unmodified
 *
 * The test drives the *real* adapter over DAP on stdio, and submits the exact
 * expressions the extension submits -- `consoleCommand` and `watchExpression`
 * from `src/inject.ts`, then the client's own transport rules over the answers.
 * So a wrong launch key, a missing `>` prefix, or an unescaped quote fails here
 * rather than inside a user's session.
 *
 * ## The test double, and its limit
 *
 * This machine has no probe. Two ST-IDE services are therefore stubbed, and
 * nothing else: `GDR_STLINK_ADAPTER` must point at an adapter whose
 * `checkDeviceName` passes through (no CMSIS device database outside VS Code),
 * whose server-start step does not require a resolved device, and whose tools
 * manager resolves no tools. Everything else -- the launch sequence, GDB spawn,
 * `target remote`, `initCommands`, the repl, `evaluate`, the reset step -- is the
 * vendor code.
 *
 * The GDB server is QEMU's gdbstub behind a shell shim, which is what the
 * adapter's `serverExe` is pointed at. This proves the adapter contract; it does
 * **not** prove an ST-LINK probe, the ST-LINK flash/reset path, or live watch.
 * A real-hardware run replaces the shim with ST-LINK_gdbserver and the stub with
 * the published extension.
 *
 * ## Skipping
 *
 * Every requirement is probed and reported through `t.skip`, matching the
 * repository's other lanes, so a partial local setup stays usable:
 * `GDR_STLINK_ADAPTER`, `GDR_STLINK_DEVICE`, `GDR_GDB` (must have Python),
 * `GDR_FREERTOS_ELF`, `GDR_QEMU`.
 */

import assert from 'node:assert/strict';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import * as net from 'node:net';
import { tmpdir } from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';

import { consoleCommand, setupCommands, watchExpression } from '../../src/inject.ts';
import { parseSnapshot } from '../../src/snapshot.ts';
import {
	dumpExpression,
	isArraySummary,
	parseDumpSize,
	parseEnvelope,
	transportPlan
} from '../../src/transport.ts';

/** The extension directory is the npm script's cwd; the runtime sits above it. */
const REPO_ROOT = process.env.GDR_REPO_ROOT ?? path.resolve(process.cwd(), '..');
const GDR_ENTRY = path.join(REPO_ROOT, 'gdr.py');
const ADAPTER = process.env.GDR_STLINK_ADAPTER ?? '';
const DEVICE = process.env.GDR_STLINK_DEVICE ?? '';
const CORE = process.env.GDR_STLINK_CORE ?? 'Cortex-M3';
const GDB = process.env.GDR_GDB ?? 'gdb';
const ELF =
	process.env.GDR_FREERTOS_ELF ??
	path.join(process.env.HOME ?? '', 'Project/gdr-fixture/freertos/mps2-an385/11.1.0/base/freertos.elf');
const QEMU = process.env.GDR_QEMU ?? 'qemu-system-arm';
const MACHINE = process.env.GDR_QEMU_MACHINE ?? 'mps2-an385';
const RTOS = 'freertos';
const VERSION = process.env.GDR_FREERTOS_VERSION ?? '11.1.0';

/** Why this lane cannot run, or `false` when it can. */
function skipReason(): string | false {
	if (ADAPTER === '') {
		return 'set GDR_STLINK_ADAPTER to the ST stlinkgdbtarget adapter script to run this lane';
	}
	if (DEVICE === '') {
		return 'set GDR_STLINK_DEVICE to the device name the adapter should launch with';
	}
	if (!existsSync(ADAPTER)) {
		return `no adapter at ${ADAPTER}`;
	}
	if (!existsSync(GDR_ENTRY)) {
		return `no GDR runtime at ${GDR_ENTRY}`;
	}
	if (!existsSync(ELF)) {
		return `no FreeRTOS fixture ELF at ${ELF}`;
	}
	for (const binary of [QEMU]) {
		const probe = spawnSync(binary, ['--version'], { encoding: 'utf8' });
		if (probe.error || probe.status !== 0) {
			return `no usable ${binary}`;
		}
	}
	// Reason: GDR runs inside GDB's Python, so a GDB without it would fail this
	// lane for a reason the lane is not about (`GDR_GDB` must be the -py3 build).
	const python = spawnSync(GDB, ['--nx', '--quiet', '--batch', '-ex', 'python print("gdr-python-ok")'], {
		encoding: 'utf8'
	});
	if (python.error || python.status !== 0 || !`${python.stdout}${python.stderr}`.includes('gdr-python-ok')) {
		return `${GDB} has no Python interpreter`;
	}
	return false;
}

/** A port the OS has just handed back, so two lanes can run side by side. */
function freePort(): Promise<number> {
	return new Promise((resolve, reject) => {
		const server = net.createServer();
		server.on('error', reject);
		server.listen(0, '127.0.0.1', () => {
			const address = server.address();
			const port = typeof address === 'object' && address !== null ? address.port : 0;
			server.close(() => resolve(port));
		});
	});
}

/** One DAP message: a length header, then a JSON body. */
interface DapMessage {
	type?: string;
	event?: string;
	command?: string;
	request_seq?: number;
	success?: boolean;
	message?: string;
	body?: {
		result?: string;
		type?: string;
		stackFrames?: { id?: number }[];
		threads?: { id?: number; name?: string }[];
		output?: string;
		category?: string;
	};
}

/**
 * A minimal DAP client over the adapter's stdio.
 *
 * Only what the contract needs: `initialize`/`launch`/`configurationDone`,
 * then `evaluate` per verb. Events are latched so a request can wait for one.
 */
class DapClient {
	private readonly child: ChildProcess;
	private buffer = Buffer.alloc(0);
	private seq = 1;
	private readonly responses = new Map<number, DapMessage>();
	private waiting: { command: number; resolve: (message: DapMessage) => void }[] = [];
	private readonly events: DapMessage[] = [];
	private eventWaiters: { name: string; resolve: (message: DapMessage) => void }[] = [];

	constructor(adapter: string) {
		this.child = spawn(process.execPath, [adapter], { stdio: ['pipe', 'pipe', 'ignore'] });
		this.child.stdout?.on('data', (chunk: Buffer) => {
			this.buffer = Buffer.concat([this.buffer, chunk]);
			for (;;) {
				const head = this.buffer.indexOf('\r\n\r\n');
				if (head < 0) {
					return;
				}
				const match = /Content-Length: (\d+)/.exec(this.buffer.subarray(0, head).toString());
				if (match === null) {
					this.buffer = this.buffer.subarray(head + 4);
					continue;
				}
				const length = Number(match[1]);
				if (this.buffer.length < head + 4 + length) {
					return;
				}
				const body = this.buffer.subarray(head + 4, head + 4 + length).toString();
				this.buffer = this.buffer.subarray(head + 4 + length);
				this.accept(JSON.parse(body) as DapMessage);
			}
		});
	}

	private accept(message: DapMessage): void {
		if (message.type === 'response') {
			const target = this.waiting.find((entry) => entry.command === message.request_seq);
			if (target !== undefined) {
				this.waiting = this.waiting.filter((entry) => entry !== target);
				target.resolve(message);
				return;
			}
			if (message.request_seq !== undefined) {
				this.responses.set(message.request_seq, message);
			}
			return;
		}
		if (message.type !== 'event') {
			return;
		}
		this.events.push(message);
		for (const waiter of [...this.eventWaiters]) {
			if (waiter.name === message.event) {
				this.eventWaiters = this.eventWaiters.filter((entry) => entry !== waiter);
				waiter.resolve(message);
			}
		}
	}

	/** All output events an adapter emitted, for a failure report. */
	output(): string {
		return this.events
			.filter((event) => event.event === 'output')
			.map((event) => event.body?.output ?? '')
			.join('');
	}

	request(command: string, args: unknown = {}): Promise<DapMessage> {
		const seq = this.seq++;
		const known = this.responses.get(seq);
		if (known !== undefined) {
			this.responses.delete(seq);
			return Promise.resolve(known);
		}
		const body = Buffer.from(JSON.stringify({ seq, type: 'request', command, arguments: args }));
		this.child.stdin?.write(`Content-Length: ${body.length}\r\n\r\n`);
		this.child.stdin?.write(body);
		return new Promise((resolve) => {
			this.waiting.push({ command: seq, resolve });
		});
	}

	waitForEvent(name: string, timeoutMs = 20000): Promise<DapMessage> {
		const known = this.events.find((event) => event.event === name);
		if (known !== undefined) {
			return Promise.resolve(known);
		}
		return new Promise((resolve, reject) => {
			const timer = setTimeout(() => reject(new Error(`no '${name}' event within ${timeoutMs} ms`)), timeoutMs);
			this.eventWaiters.push({
				name,
				resolve: (message) => {
					clearTimeout(timer);
					resolve(message);
				}
			});
		});
	}

	close(): void {
		this.child.kill('SIGKILL');
	}
}

/**
 * One `evaluate` against the adapter, spelled for it exactly as the extension
 * spells it: `consoleCommand` for the repl, `watchExpression` for a value.
 *
 * Reason: this adapter answers a repl expression it could not run with
 * `success: true` and the text `Error: could not evaluate expression`, so
 * checking `success` alone would accept a command that never reached GDB.
 */
async function evaluate(
	client: DapClient,
	frameId: number,
	context: 'repl' | 'watch',
	expression: string
): Promise<string> {
	const response = await client.request('evaluate', {
		expression: context === 'watch' ? watchExpression('stlinkgdbtarget', expression) : expression,
		frameId,
		context
	});
	assert.equal(response.success, true, `evaluate failed: ${response.message ?? expression}`);
	const result = response.body?.result ?? '';
	assert.notEqual(
		result.trim(),
		'Error: could not evaluate expression',
		`the adapter never ran ${JSON.stringify(expression)} as a GDB command`
	);
	return result;
}

test('the ST adapter hosts GDR through initCommands and the file route', async (t) => {
	const reason = skipReason();
	if (reason !== false) {
		t.skip(reason);
		return;
	}

	const root = mkdtempSync(path.join(tmpdir(), 'gdr-stlink-'));
	const loader = path.join(root, 'load.gdb');
	const dump = path.join(root, 'snapshot.json');
	const port = await freePort();
	// Reason: the adapter joins `serverCwd` and `serverExe`, so the shim is named
	// relative to the directory holding it -- an absolute `serverExe` is joined
	// onto `serverCwd` and never found.
	const shim = path.join(root, 'qemu-gdbserver.sh');
	writeFileSync(
		shim,
		[
			'#!/bin/sh',
			'# Stands in for ST-LINK_gdbserver: the adapter launches this as its server.',
			`exec ${QEMU} -M ${MACHINE} -kernel ${JSON.stringify(ELF)} -nographic -monitor none \\`,
			'  -semihosting-config enable=on,target=native -gdb "tcp::${QEMU_PORT}" -S'
		].join('\n') + '\n',
		{ mode: 0o755 }
	);

	const client = new DapClient(ADAPTER);
	try {
		const initialized = client.request('initialize', {
			adapterID: 'stlinkgdbtarget',
			linesStartAt1: true,
			columnsStartAt1: true,
			pathFormat: 'path'
		});
		const launch = client.request('launch', {
			type: 'stlinkgdbtarget',
			request: 'launch',
			name: 'gdr-stlink-lane',
			program: ELF,
			gdb: GDB,
			cwd: REPO_ROOT,
			deviceName: DEVICE,
			deviceCore: CORE,
			// Reason: live watch opens a second GDB connection to the same server.
			// QEMU's gdbstub accepts one, so the launch would hang on it; the
			// option is part of the adapter's own configuration surface.
			liveWatch: { enabled: false },
			serverExe: path.basename(shim),
			serverCwd: root,
			serverPort: port,
			serverStartupDelay: 1500,
			serverEnvironment: { QEMU_MACHINE: MACHINE, QEMU_ELF: ELF, QEMU_PORT: String(port) },
			// Reason: the extension's own two commands, through this adapter's
			// launch key. `initCommands` runs after `target remote` and before the
			// image load, which is where the gate can ask GDB about Python.
			initCommands: setupCommands(REPO_ROOT, null, loader)
		});
		await initialized;
		const launchResponse = await launch;
		assert.equal(
			launchResponse.success,
			true,
			`launch failed: ${launchResponse.message ?? client.output().slice(-600)}`
		);
		// The adapter sends `initialized` and waits for `configurationDone` before
		// it runs the program; the handshake is needed to reach a stop.
		await client.waitForEvent('initialized');
		await client.request('configurationDone', {});

		// Reason: the target resumes on `configurationDone`, and GDR only reads a
		// halted target, so the lane pauses before it asks anything.
		await new Promise((resolve) => setTimeout(resolve, 1500));
		await client.request('pause', { threadId: 1 });
		await client.waitForEvent('stopped');

		const threads = await client.request('threads', {});
		const threadId = threads.body?.threads?.[0]?.id;
		assert.equal(typeof threadId, 'number', 'the adapter reported no thread to read from');
		const stack = await client.request('stackTrace', { threadId, startFrame: 0, levels: 1 });
		const frameId = stack.body?.stackFrames?.[0]?.id;
		assert.equal(typeof frameId, 'number', 'the adapter reported no stack frame');

		// 1. The launch key reached GDB: the gate recorded what it found and the
		//    loader it wrote sources GDR, not the no-Python stub.
		assert.equal(readFileSync(`${loader}.result`, 'utf8').trim(), 'present');
		assert.match(readFileSync(loader, 'utf8'), /^source /);

		// 2. The repl needs the adapter's own prefix for a GDB CLI command.
		await evaluate(client, frameId as number, 'repl', consoleCommand('stlinkgdbtarget', `gdr init ${RTOS} ${VERSION}`));

		// 3. `$gdr_json` answers with the MI summary of a `char[N]`, which cannot
		//    carry the payload: the client's rules must see that and read a file.
		const probe = await evaluate(client, frameId as number, 'watch', '$gdr_json("probe")');
		assert.equal(transportPlan(probe).kind, 'invalid');
		assert.equal(isArraySummary(probe), true, `expected an MI array summary, got ${probe.slice(0, 120)}`);

		// 4. The file route writes the envelope where the extension can read it.
		const size = parseDumpSize(
			await evaluate(
				client,
				frameId as number,
				'watch',
				dumpExpression(dump, 'snapshot')
			)
		);
		assert.ok(size !== null && size > 0, 'the dump did not answer with a byte count');
		const envelope = parseEnvelope(readFileSync(dump, 'utf8'));
		assert.ok(envelope, 'the dumped file is not a GDR envelope');
		assert.equal(envelope.ok, true, 'GDR reported a failed snapshot');
		assert.equal(envelope.rtos, RTOS);
		assert.equal(envelope.rtos_version, VERSION);
		// Reason: the point of the file route is that real data crosses it, so the
		// parsed payload has to carry a task table, not just an ok envelope.
		const snapshot = parseSnapshot(envelope.data);
		assert.ok(snapshot, 'the dumped payload is not a snapshot');
		assert.ok(snapshot.tasks, 'the snapshot carries no task table');
		assert.ok(snapshot.tasks.rows.length > 0, 'the task table has no rows');
	} finally {
		client.close();
		rmSync(root, { recursive: true, force: true });
	}
});
