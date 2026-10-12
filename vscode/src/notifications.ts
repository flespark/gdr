/** Notification routing: build advice is inline; actionable failures are popups. */
import type { GdrEnvelope } from './transport.ts';

export type NotificationLevel = 'warning' | 'error';

/** Suppress repeated failures for the lifetime of one debug session. */
export class NotificationGate {
	private readonly seen = new Set<string>();
	accept(level: NotificationLevel, message: string): boolean {
		const key = `${level}:${message}`;
		if (this.seen.has(key)) { return false; }
		this.seen.add(key);
		return true;
	}
}

function record(value: unknown): Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function strings(value: unknown): string[] {
	return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

/** Keep legacy CLI prose out of both surfaces; report genuine runtime warnings. */
export function snapshotWarnings(envelope: GdrEnvelope): string[] {
	const data = record(envelope.data);
	const payloads = [data.tasks, data.objects, data.heap, ...Object.values(record(data.tables))];
	const messages = payloads.flatMap((payload) => strings(record(payload).messages));
	const prose = new Set(messages);
	const failures = messages.filter((text) => /\b(corrupt|truncated|failed|cannot|not initiali[sz]ed)\b/i.test(text));
	const warnings = (envelope.warnings ?? []).filter((text) => !prose.has(text));
	return [...new Set([...warnings, ...failures])];
}
