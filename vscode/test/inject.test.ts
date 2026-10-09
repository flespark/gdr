/**
 * Unit tests for the per-adapter injection plan.
 *
 * GDR is loaded into the session's own GDB, so where the loader goes depends on
 * the adapter; an adapter without a GDB console is refused with a reason rather
 * than failing later with an obscure message.
 */

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import {
	alternateKeys,
	consoleCommand,
	gdbPathKey,
	injectionPlan,
	initCommand,
	parsePythonResult,
	pythonGateCommand,
	pythonMissingMessage,
	pythonResultPath,
	REFUSED_DEBUG_TYPES,
	setupCommands,
	sourceCommand,
	supportedDebugTypes,
	watchExpression,
	type SetupCommandEntry
} from '../src/inject.ts';

const INIT = { rtos: 'rtthread', version: '4.0.5' };
const LOADER = '/tmp/gdr-load.gdb';

test('a path with spaces is the rest of the source line, not a quoted word', () => {
	assert.equal(sourceCommand('/opt/my gdr'), 'source /opt/my gdr/gdr.py');
	assert.equal(sourceCommand('/opt/we"ird\\x'), 'source /opt/we"ird\\x/gdr.py');
});

test('only a user-asserted adapter carries the unverified escape hatch', () => {
	assert.equal(
		initCommand(INIT, { allowUnverified: true }),
		'gdr init rtthread 4.0.5 --allow-unverified'
	);
	assert.equal(initCommand(INIT, { allowUnverified: false }), 'gdr init rtthread 4.0.5');
	assert.equal(initCommand(INIT), 'gdr init rtthread 4.0.5');
});

test('the Python gate sources GDR only from the configured-with-Python branch', () => {
	assert.equal(initCommand(INIT), 'gdr init rtthread 4.0.5');
	const gate = pythonGateCommand('/opt/my gdr', LOADER, INIT);
	assert.match(gate, /^pipe show configuration \|/);
	assert.match(gate, /--without-python\$/);
	assert.match(gate, /'absent'/);
	assert.match(gate, /set \$gdr_python = 0/);
	assert.match(gate, /'present'/);
	assert.match(gate, /source \/opt\/my gdr\/gdr\.py/);
	assert.match(gate, /gdr init rtthread 4\.0\.5/);
	assert.doesNotMatch(gate, /source "/);
	assert.doesNotMatch(gate, /skipping load/);
	assert.equal(parsePythonResult('absent\n'), 'absent');
	assert.equal(parsePythonResult(' present '), 'present');
	assert.equal(parsePythonResult(''), null);
	assert.equal(pythonResultPath(LOADER), `${LOADER}.result`);
	const plain = pythonGateCommand('/opt/gdr', LOADER, null);
	assert.doesNotMatch(plain, /gdr init/);
	assert.deepEqual(setupCommands('/opt/gdr', null, LOADER), [plain, `source ${LOADER}`]);
});

test('cortex-debug gets the gate and the loader on the launch path', () => {
	const plan = injectionPlan('cortex-debug', 'launch', {
		runtimePath: '/opt/gdr',
		loaderPath: LOADER,
		init: null
	});

	assert.equal(plan.supported, true);
	assert.equal(plan.entries[0].key, 'postLaunchCommands');
	assert.deepEqual(plan.entries[0].value, setupCommands('/opt/gdr', null, LOADER));
	assert.deepEqual(alternateKeys('cortex-debug'), ['postAttachCommands']);
});

test('an attach session uses the attach key when the adapter has one', () => {
	const plan = injectionPlan('cortex-debug', 'attach', {
		runtimePath: '/opt/gdr',
		loaderPath: LOADER,
		init: null
	});

	assert.equal(plan.entries[0].key, 'postAttachCommands');
});

test('cppdbg runs a console command behind -exec; cortex-debug takes it bare', () => {
	assert.equal(consoleCommand('cppdbg', 'gdr init rtthread 4.0.5'), '-exec gdr init rtthread 4.0.5');
	assert.equal(consoleCommand('cortex-debug', 'gdr init rtthread 4.0.5'), 'gdr init rtthread 4.0.5');
});

test('the cdt-gdb adapters take a console command behind > and nothing else does', () => {
	assert.equal(consoleCommand('gdbtarget', 'gdr init rtthread 4.0.5'), '>gdr init rtthread 4.0.5');
	assert.equal(consoleCommand('stlinkgdbtarget', 'gdr init rtthread 4.0.5'), '>gdr init rtthread 4.0.5');
	assert.equal(consoleCommand('cortex-debug', 'gdr init rtthread 4.0.5'), 'gdr init rtthread 4.0.5');
});

test('a cdt-gdb adapter gets init commands, which it runs after connecting', () => {
	for (const type of ['gdbtarget', 'stlinkgdbtarget']) {
		const plan = injectionPlan(type, 'launch', {
			runtimePath: '/opt/gdr',
			loaderPath: LOADER,
			init: INIT
		});

		assert.equal(plan.supported, true, type);
		assert.equal(plan.entries[0].key, 'initCommands', type);
		// Reason: these adapters read a plain string array, not the Eclipse-style
		// `{description, text, ignoreFailures}` objects cppdbg takes.
		assert.deepEqual(plan.entries[0].value, setupCommands('/opt/gdr', INIT, LOADER), type);
		assert.deepEqual(alternateKeys(type), [], type);
	}
});

test('jlinkgdbtarget keeps the key its source has not been checked against', () => {
	const plan = injectionPlan('jlinkgdbtarget', 'launch', {
		runtimePath: '/opt/gdr',
		loaderPath: LOADER,
		init: null
	});

	assert.equal(plan.entries[0].key, 'setupCommands');
});

test('a cdt-gdb expression escapes its quotes for MI, the others are left alone', () => {
	const expression = '$gdr_json("probe")';
	assert.equal(watchExpression('stlinkgdbtarget', expression), '$gdr_json(\\"probe\\")');
	assert.equal(watchExpression('gdbtarget', expression), '$gdr_json(\\"probe\\")');
	assert.equal(watchExpression('cortex-debug', expression), expression);
	assert.equal(watchExpression('cppdbg', expression), expression);
	// An expression with nothing to escape is unchanged for every adapter.
	assert.equal(watchExpression('stlinkgdbtarget', '1+1'), '1+1');
});

test('the Python hint names the attribute each adapter actually reads', () => {
	assert.equal(gdbPathKey('cortex-debug'), 'gdbPath');
	assert.equal(gdbPathKey('mcu-debug'), 'gdbPath');
	assert.equal(gdbPathKey('cppdbg'), 'miDebuggerPath');
	assert.equal(gdbPathKey('gdbtarget'), 'gdb');
	assert.equal(gdbPathKey('stlinkgdbtarget'), 'gdb');
	assert.match(pythonMissingMessage('cppdbg'), /miDebuggerPath/);
	assert.doesNotMatch(pythonMissingMessage('cppdbg'), /gdbPath/);
	assert.match(pythonMissingMessage('stlinkgdbtarget'), /gdb/);
	assert.match(pythonMissingMessage('cortex-debug'), /gdbPath/);
});

test('the supported and refused type lists stay disjoint', () => {
	const supported = supportedDebugTypes();
	assert.ok(supported.includes('stlinkgdbtarget'));
	for (const refused of REFUSED_DEBUG_TYPES) {
		assert.equal(supported.includes(refused), false, refused);
	}
});

test('an Eclipse-style adapter gets setup entries, not bare strings', () => {
	const plan = injectionPlan('cppdbg', 'launch', {
		runtimePath: '/opt/gdr',
		loaderPath: LOADER,
		init: INIT
	});
	const entries = plan.entries[0].value as SetupCommandEntry[];

	assert.equal(plan.entries[0].key, 'setupCommands');
	assert.equal(entries.length, 2);
	assert.match(entries[0].description, /check Python/);
	assert.match(entries[0].text, /gdr init rtthread 4\.0\.5/);
	assert.equal(entries[0].ignoreFailures, false);
	assert.equal(entries[1].text, `source ${LOADER}`);
	assert.equal(entries[1].ignoreFailures, false);
});

test('an adapter without a GDB console is refused with the reason', () => {
	const plan = injectionPlan('probe-rs-debug', 'launch', {
		runtimePath: '/opt/gdr',
		loaderPath: LOADER,
		init: INIT
	});

	assert.equal(plan.supported, false);
	assert.match(plan.reason ?? '', /GDB's Python/);
	assert.deepEqual(plan.entries, []);
	assert.deepEqual(alternateKeys('probe-rs-debug'), []);
});

function gdbOnPath(name: string): boolean {
	return spawnSync(name, ['--version'], { encoding: 'utf8' }).status === 0;
}

/** Send console commands the way cortex-debug wraps `postLaunchCommands`. */
function runConsole(gdb: string, commands: string[]): string {
	const script = commands
		.map((command) => `interpreter-exec console "${command.replaceAll('"', '\\"')}"`)
		.concat('quit')
		.join('\n');
	const result = spawnSync(gdb, ['--nx', '--quiet', '-i=mi'], { input: script, encoding: 'utf8' });
	return `${result.stdout ?? ''}${result.stderr ?? ''}`;
}

test('the launch commands succeed with and without Python', (t) => {
	const gdbs = ['gdb', 'arm-none-eabi-gdb'].filter(gdbOnPath);
	if (gdbs.length === 0) {
		t.skip('no gdb on PATH');
		return;
	}
	const root = mkdtempSync(join(tmpdir(), 'gdr-gate-'));
	const runtime = join(root, 'my gdr');
	const marker = join(runtime, 'gdr.py');
	const loader = join(root, 'load.gdb');
	try {
		mkdirSync(runtime, { recursive: true });
		writeFileSync(marker, 'print("GDR_GATE_OK")\n', 'utf8');
		for (const gdb of gdbs) {
			const python = spawnSync(gdb, ['--nx', '--quiet', '--batch', '-ex', 'python print("gdr-python-ok")'], {
				encoding: 'utf8'
			});
			const hasPython = python.status === 0 && `${python.stdout}${python.stderr}`.includes('gdr-python-ok');
			const output = runConsole(gdb, setupCommands(runtime, null, loader));
			assert.equal(output.includes('^error'), false, `${gdb} launch commands failed:\n${output}`);
			const loaderText = readFileSync(loader, 'utf8');
			const result = parsePythonResult(readFileSync(`${loader}.result`, 'utf8'));
			if (hasPython) {
				assert.equal(result, 'present');
				assert.match(loaderText, /^source /);
				assert.match(output, /GDR_GATE_OK/);
			} else {
				assert.equal(result, 'absent');
				assert.match(loaderText, /set \$gdr_python = 0/);
				assert.doesNotMatch(output, /GDR_GATE_OK/);
			}
		}
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});
