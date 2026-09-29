/**
 * Unit tests for the per-adapter injection plan.
 *
 * GDR is loaded into the session's own GDB, so where the loader goes depends on
 * the adapter; an adapter without a GDB console is refused with a reason rather
 * than failing later with an obscure message.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	alternateKeys,
	injectionPlan,
	initCommand,
	quotePath,
	setupCommands,
	sourceCommand,
	type SetupCommandEntry
} from '../src/inject.ts';

const INIT = { rtos: 'rtthread', version: '4.0.5' };

test('a path with spaces or quotes survives the GDB source command', () => {
	assert.equal(quotePath('/opt/my gdr'), '"/opt/my gdr"');
	assert.equal(quotePath('/opt/we"ird\\x'), '"/opt/we\\"ird\\\\x"');
	assert.equal(sourceCommand('/opt/gdr'), 'source "/opt/gdr/gdr.py"');
});

test('initialization always names the version GDR was told to expect', () => {
	assert.equal(initCommand(INIT), 'gdr init rtthread 4.0.5');
	assert.deepEqual(setupCommands('/opt/gdr', INIT), [
		'source "/opt/gdr/gdr.py"',
		'gdr init rtthread 4.0.5'
	]);
	assert.deepEqual(setupCommands('/opt/gdr', null), ['source "/opt/gdr/gdr.py"']);
});

test('cortex-debug gets its command list on the launch path', () => {
	const plan = injectionPlan('cortex-debug', 'launch', { runtimePath: '/opt/gdr', init: INIT });

	assert.equal(plan.supported, true);
	assert.deepEqual(plan.entries, [
		{
			key: 'postLaunchCommands',
			value: ['source "/opt/gdr/gdr.py"', 'gdr init rtthread 4.0.5']
		}
	]);
	assert.deepEqual(plan.replCommands, plan.entries[0].value);
	assert.deepEqual(alternateKeys('cortex-debug'), ['postAttachCommands']);
});

test('an attach session uses the attach key when the adapter has one', () => {
	const plan = injectionPlan('cortex-debug', 'attach', { runtimePath: '/opt/gdr', init: INIT });

	assert.equal(plan.entries[0].key, 'postAttachCommands');
});

test('an Eclipse-style adapter gets setup entries, not bare strings', () => {
	const plan = injectionPlan('cppdbg', 'launch', { runtimePath: '/opt/gdr', init: INIT });
	const entries = plan.entries[0].value as SetupCommandEntry[];

	assert.equal(plan.entries[0].key, 'setupCommands');
	assert.equal(entries.length, 2);
	assert.equal(entries[0].text, 'source "/opt/gdr/gdr.py"');
	assert.equal(entries[0].ignoreFailures, false);
	assert.match(entries[1].description, /GDR: gdr init rtthread 4\.0\.5/);
});

test('an adapter without a GDB console is refused with the reason', () => {
	const plan = injectionPlan('probe-rs-debug', 'launch', { runtimePath: '/opt/gdr', init: INIT });

	assert.equal(plan.supported, false);
	assert.match(plan.reason ?? '', /GDB's Python/);
	assert.deepEqual(plan.entries, []);
	assert.deepEqual(plan.replCommands, []);
	assert.deepEqual(alternateKeys('probe-rs-debug'), []);
});
