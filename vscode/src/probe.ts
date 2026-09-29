/**
 * The probe decision: which adapter, and which version, a session initializes.
 *
 * GDR reports evidence and never selects for the user, so the rules live here:
 * only an unambiguous exact candidate may be used automatically, and everything
 * else becomes a question with the evidence attached. A manual setting always
 * wins over the probe -- that is the escape hatch when a build exports nothing.
 */

export type ProbeConfidence = 'exact' | 'range' | 'undetected';

export interface ProbeCandidate {
	rtos: string;
	version: string | null;
	confidence: ProbeConfidence;
	evidence: string[];
	detail: string;
}

export interface ProbePayload {
	candidates: ProbeCandidate[];
	resolved: { rtos: string; version: string | null } | null;
}

export interface GdrSettings {
	rtos: string;
	version: string;
	autoInit: boolean;
}

export type InitDecision =
	| { kind: 'init'; rtos: string; version: string; source: 'settings' | 'probe' | 'cache' }
	| { kind: 'ask'; options: { rtos: string; version: string }[]; evidence: string[]; reason: string }
	| { kind: 'none'; reason: string };

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asConfidence(value: unknown): ProbeConfidence {
	return value === 'exact' || value === 'range' || value === 'undetected' ? value : 'undetected';
}

/** Read the probe payload out of an envelope's data, or `null`. */
export function parseProbePayload(data: unknown): ProbePayload | null {
	if (!isRecord(data) || !Array.isArray(data.candidates)) {
		return null;
	}
	const candidates: ProbeCandidate[] = [];
	for (const item of data.candidates) {
		if (!isRecord(item) || typeof item.rtos !== 'string') {
			continue;
		}
		candidates.push({
			rtos: item.rtos,
			version: typeof item.version === 'string' ? item.version : null,
			confidence: asConfidence(item.confidence),
			evidence: Array.isArray(item.evidence)
				? item.evidence.filter((line): line is string => typeof line === 'string')
				: [],
			detail: typeof item.detail === 'string' ? item.detail : ''
		});
	}
	const resolvedRaw = data.resolved;
	const resolved =
		isRecord(resolvedRaw) && typeof resolvedRaw.rtos === 'string'
			? { rtos: resolvedRaw.rtos, version: typeof resolvedRaw.version === 'string' ? resolvedRaw.version : null }
			: null;
	return { candidates, resolved };
}

/** Every evidence line of a payload, for the UI to show with a question. */
export function evidenceLines(payload: ProbePayload): string[] {
	return payload.candidates.flatMap((candidate) =>
		candidate.evidence.map((line) => `${candidate.rtos}: ${line}`)
	);
}

/** Candidates that carry a concrete version, in payload order. */
function versionedOptions(payload: ProbePayload, rtos?: string): { rtos: string; version: string }[] {
	return payload.candidates
		.filter((candidate) => candidate.version !== null && (rtos === undefined || candidate.rtos === rtos))
		.map((candidate) => ({ rtos: candidate.rtos, version: candidate.version as string }));
}

/**
 * Decide what the session should do, never guessing a version.
 *
 * Order of authority: explicit settings, then a cached answer from the same
 * launch configuration, then the probe. An unusable probe produces `ask` (with
 * the evidence) or `none`, never a silent default.
 */
export function decideInit(
	payload: ProbePayload | null,
	settings: GdrSettings,
	cached?: { rtos: string; version: string } | null
): InitDecision {
	if (settings.rtos !== '' && settings.version !== '') {
		return { kind: 'init', rtos: settings.rtos, version: settings.version, source: 'settings' };
	}
	if (cached) {
		return { kind: 'init', rtos: cached.rtos, version: cached.version, source: 'cache' };
	}
	if (settings.rtos !== '') {
		// The RTOS is pinned but its version is not: offer only its candidates
		// rather than asking the user to retype the adapter.
		const options = versionedOptions(payload ?? { candidates: [], resolved: null }, settings.rtos);
		if (options.length > 0) {
			return {
				kind: 'ask',
				options,
				evidence: payload ? evidenceLines(payload) : [],
				reason: `several versions are possible for ${settings.rtos}`
			};
		}
		return {
			kind: 'none',
			reason: `gdr.rtos is set to ${settings.rtos} but gdr.version is empty and the probe found no version`
		};
	}
	if (payload === null) {
		return { kind: 'none', reason: 'the probe returned no readable payload' };
	}
	if (payload.candidates.length === 0) {
		return { kind: 'none', reason: 'no supported RTOS was detected on this target' };
	}
	if (payload.resolved?.version) {
		if (!settings.autoInit) {
			return {
				kind: 'ask',
				options: [{ rtos: payload.resolved.rtos, version: payload.resolved.version }],
				evidence: evidenceLines(payload),
				reason: 'automatic initialization is disabled (gdr.autoInit)'
			};
		}
		return { kind: 'init', rtos: payload.resolved.rtos, version: payload.resolved.version, source: 'probe' };
	}
	const options = versionedOptions(payload);
	if (options.length === 1) {
		return {
			kind: 'ask',
			options,
			evidence: evidenceLines(payload),
			reason: 'the probe found one adapter but its version is not unambiguous'
		};
	}
	return {
		kind: 'ask',
		options,
		evidence: evidenceLines(payload),
		reason: options.length === 0 ? 'no version could be read from this build' : 'several adapters or versions match'
	};
}

/** The launch-configuration shape this cache key reads (no vscode import). */
export interface LaunchIdentity {
	name?: string;
	type?: string;
	program?: string;
	executable?: string;
	coreConfigs?: { programBinary?: string }[];
}

/**
 * A stable cache key for "this launch configuration", or `null`.
 *
 * The plugin cannot read the ELF build-id through DAP, so the identity is the
 * program path plus the configuration name: a reflash in place keeps the
 * answer, while switching firmware invalidates it.
 */
export function cacheKey(identity: LaunchIdentity | undefined): string | null {
	if (!identity) {
		return null;
	}
	const program =
		identity.executable ??
		identity.program ??
		identity.coreConfigs?.find((core) => typeof core.programBinary === 'string')?.programBinary;
	if (typeof program !== 'string' || program === '') {
		return null;
	}
	return `${identity.type ?? ''}|${identity.name ?? ''}|${program}`;
}
