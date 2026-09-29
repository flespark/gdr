/**
 * How a debug session receives GDR, per debug adapter.
 *
 * The plugin works only after the debug session starts: GDR is loaded into the
 * session's own GDB through the adapter's launch
 * hook when it has one, and through the debug console otherwise. GDR itself
 * never drives the target, and neither does this module -- it only spells the
 * two commands a session needs.
 *
 * GDR runs inside GDB's Python interpreter, so an adapter that does not expose
 * a GDB console cannot host it at all; such a session is reported as
 * unsupported with the reason, instead of failing later with an obscure error.
 */

/** One `setupCommands` entry, as Eclipse-style adapters spell it. */
export interface SetupCommandEntry {
	description: string;
	text: string;
	ignoreFailures: boolean;
}

/** What a launch-configuration key may hold for an injection. */
export type InjectValue = string[] | SetupCommandEntry[];

/** A launch-configuration entry to add, already shaped for its adapter. */
export interface InjectEntry {
	key: string;
	value: InjectValue;
}

export interface InjectionPlan {
	supported: boolean;
	reason?: string;
	entries: InjectEntry[];
	/** Commands to run through the debug console when no hook was applied. */
	replCommands: string[];
}

/** Adapters that own a GDB process, and the key their launch config uses. */
const GDB_ADAPTERS: Record<string, string> = {
	'cortex-debug': 'postLaunchCommands',
	'mcu-debug': 'postLaunchCommands',
	cppdbg: 'setupCommands',
	gdbtarget: 'setupCommands',
	stlinkgdbtarget: 'setupCommands',
	jlinkgdbtarget: 'setupCommands'
};

/** Adapters whose *attach* path uses a different key than their launch path. */
const GDB_ATTACH_KEYS: Record<string, string> = {
	'cortex-debug': 'postAttachCommands',
	'mcu-debug': 'postAttachCommands'
};

/** Quote one path for GDB's `source` command. */
export function quotePath(path: string): string {
	return `"${path.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
}

/** The command that loads GDR from *runtimePath*. */
export function sourceCommand(runtimePath: string, fileName = 'gdr.py'): string {
	return `source ${quotePath(`${runtimePath}/${fileName}`)}`;
}

/**
 * The command that initializes one adapter.
 *
 * The version is always explicit: GDR does not auto-detect, and the plugin's
 * probe has already resolved it before anything is injected.
 */
export function initCommand(init: { rtos: string; version: string }): string {
	return `gdr init ${init.rtos} ${init.version}`;
}

/** The two commands a session needs, in order. */
export function setupCommands(runtimePath: string, init: { rtos: string; version: string } | null): string[] {
	const commands = [sourceCommand(runtimePath)];
	if (init !== null) {
		commands.push(initCommand(init));
	}
	return commands;
}

/**
 * Plan the injection for one debug session.
 *
 * `init` is `null` while the adapter is still undecided: GDR is loaded either
 * way, because the probe that decides it runs inside that GDB process.
 */
export function injectionPlan(
	debugType: string,
	request: 'launch' | 'attach',
	options: { runtimePath: string; init: { rtos: string; version: string } | null }
): InjectionPlan {
	const launchKey = GDB_ADAPTERS[debugType];
	const commands = setupCommands(options.runtimePath, options.init);
	if (launchKey === undefined) {
		return {
			supported: false,
			reason:
				`${debugType} does not expose a GDB console; GDR runs inside GDB's Python ` +
				`interpreter, so this adapter cannot host it`,
			entries: [],
			replCommands: []
		};
	}
	const key = request === 'attach' ? (GDB_ATTACH_KEYS[debugType] ?? launchKey) : launchKey;
	return {
		supported: true,
		entries: [{ key, value: launchEntries(key, commands) }],
		replCommands: commands
	};
}

/**
 * Extra launch-configuration keys this adapter may accept the same commands on.
 *
 * A `launch`-only session still needs the attach spelling prepared when the
 * user later switches `request` to `attach` without rerunning the provider.
 */
export function alternateKeys(debugType: string): string[] {
	const keys = new Set<string>();
	const launchKey = GDB_ADAPTERS[debugType];
	if (launchKey === undefined) {
		return [];
	}
	keys.add(launchKey);
	const attachKey = GDB_ATTACH_KEYS[debugType];
	if (attachKey !== undefined) {
		keys.add(attachKey);
	}
	return [...keys].filter((key) => key !== launchKey);
}

/** Build the value an adapter's configuration key takes for these commands. */
function launchEntries(key: string, commands: string[]): InjectValue {
	if (key === 'setupCommands') {
		return commands.map((text) => ({
			description: `GDR: ${text}`,
			text,
			ignoreFailures: false
		}));
	}
	return commands;
}
