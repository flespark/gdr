/**
 * Closed-loop check of the plugin's data path against a real debug session.
 *
 * The extension's data channel is `$gdr_json`, whose value is a *target* string:
 * GDB can only materialize it with an inferior present, so this test boots the
 * same QEMU profile the repository's Python lanes use, connects the same
 * Python-enabled GDB, sources the repository's `gdr.py`, and runs the exact
 * expressions the extension issues. The payload then goes through the *real*
 * transport and probe parsers, so a change on either side of the contract fails
 * here instead of inside a user's session.
 *
 * The test skips (never fails) when the toolchain or fixture is absent, matching
 * the repository's other lanes. This test asserts the data path; a panel
 * assertion needs a webview and is not covered here.
 */

import assert from 'node:assert/strict';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { existsSync, readFileSync, rmSync } from 'node:fs';
import * as net from 'node:net';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';

import { parseProbePayload } from '../../src/probe.ts';
import { parseSnapshot, type PanelSnapshot } from '../../src/snapshot.ts';
import { renderApp, snapshotHints } from '../../src/webview/render.ts';
import {
	describeFailure,
	parseEnvelope,
	reassembleChunks,
	transportPlan,
	type GdrEnvelope
} from '../../src/transport.ts';

/** The extension directory is the npm script's cwd; the runtime sits above it. */
const REPO_ROOT = process.env.GDR_REPO_ROOT ?? path.resolve(process.cwd(), '..');
const GDB = process.env.GDR_GDB ?? 'arm-none-eabi-gdb-py3';
const QEMU = process.env.GDR_QEMU ?? 'qemu-system-arm';
const CACHE =
	process.env.RT_THREAD_FIXTURE_CACHE ??
	path.join(process.env.HOME ?? '', 'Project/gdr-fixture/rtthread');
const ELF = process.env.GDR_PROBE_ELF ?? path.join(CACHE, 'cortex-a9', '4.0.5', 'rtthread.elf');
const GDR_ENTRY = path.join(REPO_ROOT, 'gdr.py');
const MACHINE = 'vexpress-a9';

function skipReason(): string | false {
	if (!existsSync(GDR_ENTRY)) {
		return `no GDR runtime at ${GDR_ENTRY}`;
	}
	if (!existsSync(ELF)) {
		return `no fixture ELF at ${ELF}`;
	}
	for (const binary of [GDB, QEMU]) {
		const probe = spawnSync(binary, ['--version'], { encoding: 'utf8' });
		if (probe.error || probe.status !== 0) {
			return `no usable ${binary}`;
		}
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

async function waitForPort(port: number, timeoutMs = 15000): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	for (;;) {
		const connected = await new Promise<boolean>((resolve) => {
			const socket = net.connect({ port, host: '127.0.0.1' });
			socket.once('connect', () => {
				socket.destroy();
				resolve(true);
			});
			socket.once('error', () => resolve(false));
		});
		if (connected) {
			return;
		}
		if (Date.now() > deadline) {
			throw new Error(`QEMU did not open its GDB port ${port} within ${timeoutMs} ms`);
		}
		await new Promise((resolve) => setTimeout(resolve, 100));
	}
}

/**
 * One `$gdr_json(...)` expression with its arguments quoted for GDB.
 *
 * The payload is printed by GDB's own Python (`Value.string()`), not by the
 * value printer, so the print-element limit cannot truncate it and hide a
 * transport defect.
 */
function expression(verb: string, args: string[] = []): string {
	return `$gdr_json(${[verb, ...args].map((value) => JSON.stringify(value)).join(', ')})`;
}

function evaluateWithPort(port: number, expr: string): string {
	const script = `import gdb\nprint(gdb.parse_and_eval(${JSON.stringify(expr)}).string())`;
	const run = spawnSync(
		GDB,
		[
			'--nx',
			'--quiet',
			'--batch',
			'-ex',
			`file ${ELF}`,
			'-ex',
			`target remote :${port}`,
			'-ex',
			`source ${GDR_ENTRY}`,
			'-ex',
			`python ${script}`
		],
		{ encoding: 'utf8' }
	);
	if (run.status !== 0) {
		throw new Error(`GDB failed (${run.status}): ${run.stderr || run.stdout}`);
	}
	const line = run.stdout
		.split('\n')
		.map((candidate) => candidate.trim())
		.find((candidate) => candidate.startsWith('{'));
	if (!line) {
		throw new Error(`no JSON payload in GDB output:\n${run.stdout}`);
	}
	return line;
}

/** Boot one QEMU instance and run the body against its GDB port. */
async function withSession<T>(body: (port: number) => T): Promise<T> {
	const port = await freePort();
	const qemu: ChildProcess = spawn(
		QEMU,
		['-M', MACHINE, '-kernel', ELF, '-nographic', '-monitor', 'none', '-gdb', `tcp::${port}`],
		{ stdio: 'ignore' }
	);
	try {
		await waitForPort(port);
		return await body(port);
	} finally {
		qemu.kill('SIGKILL');
	}
}

/**
 * Fetch one verb's payload text through the client's own transport rules.
 *
 * Returns which branch carried it, so a test asserts the branch it exercised
 * instead of assuming one.
 */
function fetchPayload(port: number, verb: string, args: string[] = []): { kind: 'inline' | 'chunked'; text: string } {
	const first = evaluateWithPort(port, expression(verb, args));
	const plan = transportPlan(first);
	if (plan.kind === 'inline') {
		return { kind: 'inline', text: plan.payload };
	}
	if (plan.kind === 'invalid') {
		throw new Error(`unrecognized payload for ${verb}: ${first.slice(0, 200)}`);
	}
	const slices: string[] = [];
	for (let index = 1; index <= plan.meta.total; index += 1) {
		slices.push(
			evaluateWithPort(
				port,
				`$gdr_json_chunk(${index}, ${[verb, ...args].map((value) => JSON.stringify(value)).join(', ')})`
			)
		);
	}
	return { kind: 'chunked', text: reassembleChunks(plan.meta, slices) };
}

/** Narrow a parser result the test expects to be present. */
function must<T>(value: T | null, message: string): T {
	if (value === null) {
		throw new Error(message);
	}
	return value;
}

function mustEnvelope(text: string): GdrEnvelope {
	const envelope = parseEnvelope(text);
	assert.ok(envelope, `not a GDR envelope: ${text.slice(0, 200)}`);
	return envelope;
}

const reason = skipReason();

function renderableSnapshot(envelope: GdrEnvelope): PanelSnapshot {
	const data = must(parseSnapshot(envelope.data), 'a real snapshot must parse');
	return {
		rtos: envelope.rtos ?? null,
		rtosVersion: envelope.rtos_version ?? null,
		gdrVersion: envelope.gdr_version ?? null,
		generation: envelope.generation ?? null,
		elapsedMs: envelope.elapsed_ms ?? null,
		collectedAt: Date.now(),
		warnings: envelope.warnings ?? [],
		noHeap: false,
		noDetails: false,
		data
	};
}

/**
 * A real RT-Thread snapshot rendered by the panel renderer.
 *
 * One batch GDB session loads the fixture, initializes the adapter, and asks
 * the snapshot through GDR's file bypass (`--out`), which is the Phase 4
 * mechanism for the very large payloads; the written envelope then goes
 * through the real parsers and the real renderer. This is the closest a
 * panel assertion gets without an Electron host: real task rows, real column
 * alignment, real hint text.
 */
test('a real snapshot renders task rows, alignment and hints', { skip: reason }, async () => {
	await withSession((port) => {
		const outFile = path.join(os.tmpdir(), `gdr-snapshot-integration-${process.pid}.json`);
		try {
			const run = spawnSync(
				GDB,
				[
					'--nx',
					'--quiet',
					'--batch',
					'-ex',
					`file ${ELF}`,
					'-ex',
					`target remote :${port}`,
					'-ex',
					`source ${GDR_ENTRY}`,
					'-ex',
					'gdr init rtthread 4.0.5',
					'-ex',
					`gdr api snapshot --json --out ${outFile}`,
					'-ex',
					'quit'
				],
				{ encoding: 'utf8' }
			);
			assert.equal(run.status, 0, `the snapshot batch failed: ${run.stderr}`);

			const envelope = parseEnvelope(readFileSync(outFile, 'utf8'));
			assert.ok(envelope, 'the file bypass must deliver one envelope');
			assert.equal(envelope.ok, true, describeFailure(envelope));
			const snapshot = renderableSnapshot(envelope);
			assert.ok(snapshot.data.tasks, 'the snapshot carries a task table');
			const tasks = snapshot.data.tasks;
			assert.ok(tasks.rows.length > 0, 'the task table has rows');

			const html = renderApp({
				sessions: [{ id: 's0', name: 'cortex-a9', status: 'ok', message: null, snapshot }],
				activeId: 's0'
			});

			// The first task's name (without the running-task asterisk) appears.
			const firstName = tasks.rows[0][0].replace(/ \*$/, '');
			assert.ok(html.includes(firstName), `task name ${firstName} must render`);
			assert.ok(html.includes('gdr-right'), 'numeric columns are right-aligned');
			assert.ok(html.includes('data-address='), 'address cells are navigable');
			assert.ok(html.includes('gdr-table'), 'the table markup is present');

			// Panel-visible messages: deduced either from hint bars or, when the
			// kernel built clean, an explicit zero-length check.
			const hints = snapshotHints(snapshot);
			for (const hint of hints) {
				assert.ok(html.includes(hint.text), `the hint ${hint.text} must be rendered`);
			}
		} finally {
			try {
				rmSync(outFile, { force: true });
			} catch {
				// A stray temp file is acceptable next to a failing assertion.
			}
		}
	});
});

test('probe answers with real evidence before any adapter exists', { skip: reason }, async () => {
	await withSession((port) => {
		const { text } = fetchPayload(port, 'probe');
		const envelope = mustEnvelope(text);

		assert.equal(envelope.ok, true, 'the probe must answer before initialization');
		assert.equal(envelope.schema_version, 1);
		assert.equal(envelope.rtos, null, 'no adapter is initialized at this point');

		const payload = must(parseProbePayload(envelope.data), 'the probe payload must be readable');
		assert.deepEqual(
			payload.candidates.map((candidate) => candidate.rtos),
			['rtthread'],
			'the fixture image is an RT-Thread build'
		);
		const candidate = payload.candidates[0];
		assert.ok(candidate.evidence.length > 0, 'a candidate must carry its evidence');
		assert.ok(
			candidate.evidence.some((line) => line.startsWith('symbol: ')),
			'the evidence names the symbols it matched'
		);
		assert.equal(candidate.detail.length > 0, candidate.confidence !== 'exact');
	});
});

test('an adapter-less session answers other verbs with a coded error', { skip: reason }, async () => {
	// The plugin relies on this exact code to decide that initialization did not
	// take effect, so it is asserted against a real session rather than a mock.
	await withSession((port) => {
		const envelope = mustEnvelope(fetchPayload(port, 'tasks').text);

		assert.equal(envelope.ok, false);
		assert.equal(envelope.error?.code, 'not_initialized');
		assert.match(envelope.error?.message ?? '', /gdr init/);

		const unknown = mustEnvelope(fetchPayload(port, 'bogus').text);
		assert.equal(
			unknown.error?.code,
			'not_initialized',
			'the missing session is reported before the vocabulary'
		);
	});
});
