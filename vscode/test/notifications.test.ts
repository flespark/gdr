/** Build advice is explicit; CLI prose is not an alert, repeated faults are gated. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { NotificationGate, snapshotWarnings } from '../src/notifications.ts';

test('notification gate deduplicates a cause within one session, not across sessions', () => {
	const gate = new NotificationGate();
	assert.equal(gate.accept('warning', 'target unavailable'), true);
	assert.equal(gate.accept('warning', 'target unavailable'), false);
	assert.equal(gate.accept('error', 'target unavailable'), true);
	assert.equal(new NotificationGate().accept('warning', 'target unavailable'), true);
});

test('runtime warning routing drops duplicated CLI provenance but retains verification and read failures', () => {
	const registry = 'object counts come from the kernel object registry (rt_object_get_information)';
	const corrupt = 'heap walk corrupt; later blocks not drawn';
	const warnings = snapshotWarnings({ schema_version: 2, ok: true,
		warnings: [registry, 'target version could not be verified', corrupt],
		data: { objects: { messages: [registry] }, tasks: { messages: ['Kernel tick: 100'] },
			heap: { messages: [corrupt] }, tables: { timer: { messages: ['timer subsystem not initialised (pxCurrentTimerList == NULL)'] } },
			capabilities: { kinds: { task: { note: 'CPU/Bind appear only on SMP builds' } } },
			build_hints: ['enable configUSE_TRACE_FACILITY'] }
	});
	assert.deepEqual(warnings, ['target version could not be verified', corrupt, 'timer subsystem not initialised (pxCurrentTimerList == NULL)']);
});
