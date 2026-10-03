/**
 * How a debug session receives GDR, per debug adapter.
 *
 * GDR is loaded into the session's own GDB through that adapter's launch
 * hook. The first injected command checks whether this GDB was built with
 * Python and writes a loader plus a result file; the second command sources
 * that loader. A GDB without Python gets a loader that does not fail the
 * launch. The extension reads the result file and shows the failure in the
 * panel. The first stop does not check Python or source GDR again.
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

/**
 * Loader body when Python is missing.
 *
 * It has to succeed so cortex-debug does not abort the launch. The user-facing
 * explanation is the result file, which the extension shows in the panel.
 */
const ABSENT_LOADER = 'set $gdr_python = 0';

/** What the launch-time Python check recorded. */
export type PythonLaunchResult = 'present' | 'absent';

/** Quote *value* for the POSIX shell GDB's `pipe` command invokes. */
function shSingle(value: string): string {
	return `'${value.replaceAll("'", `'\\''`)}'`;
}

/**
 * Load *filePath* with GDB's `source`.
 *
 * The rest of the line is the filename. Quotes become part of the name on
 * GDB 16 and later, so a path is written raw, spaces included.
 */
export function sourceFile(filePath: string): string {
	return `source ${filePath}`;
}

/** The command that loads GDR from *runtimePath*. */
export function sourceCommand(runtimePath: string, fileName = 'gdr.py'): string {
	return sourceFile(`${runtimePath}/${fileName}`);
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

/** Sibling of *loaderPath* where the gate records `present` or `absent`. */
export function pythonResultPath(loaderPath: string): string {
	return `${loaderPath}.result`;
}

/** Parse the launch-time result file. Anything else is not a result yet. */
export function parsePythonResult(text: string): PythonLaunchResult | null {
	const value = text.trim();
	if (value === 'present' || value === 'absent') {
		return value;
	}
	return null;
}

/**
 * The loader path this extension stored on a resolved debug configuration.
 *
 * The session id does not exist yet when the configuration is resolved, so the
 * path rides on the configuration the session later reports.
 */
export function launchLoaderPath(configuration: { [key: string]: unknown }): string | null {
	const value = configuration.gdrLoaderPath;
	return typeof value === 'string' && value !== '' ? value : null;
}

/**
 * The launch command that decides whether the following `source` loads GDR.
 *
 * The `python` command itself returns an error when GDB was built without an
 * interpreter, and cortex-debug aborts the launch on any `postLaunchCommands`
 * failure. `show configuration` succeeds either way. The shell reads that
 * output and writes *loaderPath* plus `pythonResultPath(loaderPath)`:
 * `present` and a `source` of GDR when Python was configured in, or `absent`
 * and a command that does not fail the launch when it was not.
 */
export function pythonGateCommand(
	runtimePath: string,
	loaderPath: string,
	init: { rtos: string; version: string } | null
): string {
	const lines = [sourceCommand(runtimePath)];
	if (init !== null) {
		lines.push(initCommand(init));
	}
	const present = lines.map((line) => shSingle(line)).join(' ');
	const resultPath = shSingle(pythonResultPath(loaderPath));
	const loader = shSingle(loaderPath);
	return (
		'pipe show configuration | ' +
		`if grep -q -E -- '--without-python$|--with-python=no$'; then ` +
		`printf '%s\\n' 'absent' > ${resultPath}; ` +
		`printf '%s\\n' ${shSingle(ABSENT_LOADER)} > ${loader}; ` +
		`else printf '%s\\n' 'present' > ${resultPath}; ` +
		`printf '%s\\n' ${present} > ${loader}; ` +
		'fi'
	);
}

/**
 * Commands a launch runs, in order: the Python gate, then the loader it wrote.
 *
 * `init` is `null` while the adapter is still undecided. The gate still loads
 * GDR, because the probe that decides the adapter runs inside that GDB.
 */
export function setupCommands(
	runtimePath: string,
	init: { rtos: string; version: string } | null,
	loaderPath: string
): string[] {
	return [pythonGateCommand(runtimePath, loaderPath, init), sourceFile(loaderPath)];
}

/** Why *debugType* cannot host GDR, or `null` when it can. */
export function unsupportedReason(debugType: string): string | null {
	if (GDB_ADAPTERS[debugType] !== undefined) {
		return null;
	}
	return (
		`${debugType} does not expose a GDB console; GDR runs inside GDB's Python ` +
		`interpreter, so this adapter cannot host it`
	);
}

/**
 * Plan the injection for one debug session.
 *
 * *loaderPath* is where the gate writes the script the next command sources.
 * It must be a path the GDB process can create (the machine running GDB).
 */
export function injectionPlan(
	debugType: string,
	request: 'launch' | 'attach',
	options: { runtimePath: string; loaderPath: string; init: { rtos: string; version: string } | null }
): InjectionPlan {
	const launchKey = GDB_ADAPTERS[debugType];
	if (launchKey === undefined) {
		return {
			supported: false,
			reason: unsupportedReason(debugType) ?? `${debugType} cannot host GDR`,
			entries: []
		};
	}
	const commands = setupCommands(options.runtimePath, options.init, options.loaderPath);
	const key = request === 'attach' ? (GDB_ATTACH_KEYS[debugType] ?? launchKey) : launchKey;
	return {
		supported: true,
		entries: [{ key, value: launchEntries(key, commands) }]
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
		const descriptions = ['GDR: check Python before loading', 'GDR: source the loader'];
		return commands.map((text, index) => ({
			description: descriptions[index] ?? 'GDR',
			text,
			// Reason: the gate command succeeds when Python is missing (it
			// records `absent` instead of sourcing). A later real failure, such
			// as a missing gdr.py, still stops the launch.
			ignoreFailures: false
		}));
	}
	return commands;
}
