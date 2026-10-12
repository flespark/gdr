/**
 * Where the GDR runtime lives, and which version of it this bundle carries.
 *
 * The extension ships a copy of the Python runtime rather than downloading one:
 * the plugin's parsing depends on the envelope schema, so the two must travel
 * together. `gdr.runtimePath` overrides the bundled copy for development, and
 * the bundled manifest is checked against the schema the client was built for.
 */

/// <reference types="node" />
// Reason: this subproject keeps its own node_modules, so an editor or `tsc`
// invocation whose analysis root is the repository root would otherwise not
// resolve the Node builtins this file uses.

import { readFileSync, statSync } from 'node:fs';
import * as path from 'node:path';

import { GDR_SCHEMA_VERSION } from './transport.ts';

/** `resources/gdr/VERSION`, written by `scripts/vendor-gdr.sh`. */
export interface RuntimeManifest {
	version: string;
	schema_version: number;
}

export interface RuntimeLocation {
	/** Directory holding `gdr.py` and the packages. */
	directory: string;
	/** `gdr.py` itself. */
	entryPoint: string;
	manifest: RuntimeManifest | null;
	/** True when the directory came from the setting rather than the bundle. */
	overridden: boolean;
}

export interface RuntimeProblem {
	kind: 'missing' | 'schema' | 'manifest';
	message: string;
}

/** Read the bundled manifest, or `null` when it is absent or malformed. */
export function readManifest(directory: string): RuntimeManifest | null {
	try {
		const raw = readFileSync(path.join(directory, 'VERSION'), 'utf8');
		const parsed: unknown = JSON.parse(raw);
		if (typeof parsed !== 'object' || parsed === null) {
			return null;
		}
		const record = parsed as Record<string, unknown>;
		if (typeof record.version !== 'string' || typeof record.schema_version !== 'number') {
			return null;
		}
		return { version: record.version, schema_version: record.schema_version };
	} catch {
		return null;
	}
}

/**
 * Resolve the runtime directory for one session.
 *
 * A configured path is used as-is (so a developer's checkout works even when it
 * carries no manifest); the bundled copy must carry one, because otherwise the
 * client cannot tell which schema it is talking to.
 */
export function locateRuntime(bundledDirectory: string, configuredPath: string): RuntimeLocation {
	const overridden = configuredPath.trim() !== '';
	const directory = overridden ? configuredPath.trim() : bundledDirectory;
	return {
		directory,
		entryPoint: path.join(directory, 'gdr.py'),
		manifest: readManifest(directory),
		overridden
	};
}

/**
 * Check a runtime location against the schema this client understands.
 *
 * Only a schema mismatch is fatal: GDR's envelope is the contract, and a client
 * that guessed at a different one would mis-render data instead of failing.
 */
export function checkRuntime(location: RuntimeLocation): RuntimeProblem | null {
	try {
		if (!statSync(location.entryPoint).isFile()) { throw new Error('not a file'); }
	} catch {
		return { kind: 'missing', message: `GDR runtime entry point is missing or inaccessible: ${location.entryPoint}; check gdr.runtimePath or reinstall the extension` };
	}
	if (location.overridden) {
		return null;
	}
	if (location.manifest === null) {
		return {
			kind: 'manifest',
			message:
				`the bundled GDR runtime at ${location.directory} has no VERSION manifest; ` +
				`run \`npm run vendor\` in the extension directory`
		};
	}
	if (location.manifest.schema_version !== GDR_SCHEMA_VERSION) {
		return {
			kind: 'schema',
			message:
				`the bundled GDR runtime speaks schema ${location.manifest.schema_version} ` +
				`but this extension was built for ${GDR_SCHEMA_VERSION}; ` +
				`reinstall a matching extension version`
		};
	}
	return null;
}
