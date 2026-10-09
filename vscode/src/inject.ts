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

/**
 * Adapters that own a GDB process, and the launch key that takes extra GDB commands.
 *
 * Every key was read from that adapter's own `package.json`. cppdbg reads
 * `setupCommands`; cortex-debug reads `postLaunchCommands`. The cdt-gdb family
 * (`gdbtarget` and STM32Cube's `stlinkgdbtarget`) reads `initCommands` and has no
 * `setupCommands` at all, so a `setupCommands` entry there is silently ignored and
 * GDR never loads. `initCommands` runs after the target is connected and before
 * any image load.
 */
const GDB_ADAPTERS: Record<string, string> = {
	'cortex-debug': 'postLaunchCommands',
	'mcu-debug': 'postLaunchCommands',
	cppdbg: 'setupCommands',
	gdbtarget: 'initCommands',
	stlinkgdbtarget: 'initCommands',
	// Reason: this adapter's launch keys are not verified against its source.
	jlinkgdbtarget: 'setupCommands'
};

/**
 * Types that cannot host GDR because their debug console is not GDB.
 *
 * probe-rs-debug is a native DAP whose REPL answers a fixed command set (`bt`,
 * `break`, `p`, `dump`, ...) and rejects everything else as an unknown command;
 * cspy drives C-SPY through its own backend and exposes no GDB. They are named
 * here so a session of that type reports why instead of staying silent.
 */
export const REFUSED_DEBUG_TYPES: readonly string[] = ['probe-rs-debug', 'cspy'];

/** Every adapter type this extension injects GDR into. */
export function supportedDebugTypes(): string[] {
	return Object.keys(GDB_ADAPTERS);
}

/**
 * Every debug type the tracker subscribes to.
 *
 * The supported types plus the refused ones, so a session GDR cannot host still
 * reaches the panel and reports why instead of never being seen at all.
 */
export function trackedDebugTypes(): string[] {
	return [...supportedDebugTypes(), ...REFUSED_DEBUG_TYPES];
}

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
 * probe has already resolved it before anything is injected. *allowUnverified*
 * adds GDR's escape hatch, for the one case where the user asserted the
 * adapter themselves because the target shows no RTOS symbol at all.
 */
export function initCommand(
	init: { rtos: string; version: string },
	options: { allowUnverified?: boolean } = {}
): string {
	const flag = options.allowUnverified === true ? ' --allow-unverified' : '';
	return `gdr init ${init.rtos} ${init.version}${flag}`;
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

/**
 * The prefix each adapter's Debug Console (the `repl` evaluate) needs before a
 * GDB CLI command. Without it the text is evaluated as an expression.
 *
 * Reason: cppdbg documents `-exec` for that (plain `gdr init ...` would be
 * evaluated as an expression and never run). The cdt-gdb family (`gdbtarget`,
 * `stlinkgdbtarget`) takes `>` on the same line instead, and rejects a bare
 * command with `Error: could not evaluate expression`.
 */
const CONSOLE_PREFIX = new Map<string, string>([
	['cppdbg', '-exec '],
	['gdbtarget', '>'],
	['stlinkgdbtarget', '>']
]);

/**
 * Spell *command* so the adapter's `repl` evaluate runs it as a GDB command.
 *
 * Adapters with no prefix run a bare command line; see `CONSOLE_PREFIX` for the
 * ones that need a marker.
 */
export function consoleCommand(debugType: string, command: string): string {
	return `${CONSOLE_PREFIX.get(debugType) ?? ''}${command}`;
}

/**
 * Spell *expression* so the adapter's `evaluate` reaches GDB for a variable.
 *
 * Reason: a cdt-gdb adapter interpolates the expression into an MI c-string
 * without escaping it (`quote(expression)`), so a `$gdr_json("probe")` with its
 * own quotes arrives as `"$gdr_json("probe")"` and GDB answers `Problem parsing
 * arguments`. Writing the quotes as `\"` lets MI's own unescaping restore them.
 * Adapters that already escape (cortex-debug, cppdbg) must be left alone.
 */
export function watchExpression(debugType: string, expression: string): string {
	if (debugType !== 'gdbtarget' && debugType !== 'stlinkgdbtarget') {
		return expression;
	}
	return expression.replaceAll('"', '\\"');
}

/**
 * The launch attribute naming *debugType*'s GDB executable.
 *
 * Read from each adapter's own `package.json`; the extension names this key when
 * it has to tell the user which binary to point at a Python-enabled GDB.
 */
export function gdbPathKey(debugType: string): string {
	switch (debugType) {
		case 'cppdbg':
			return 'miDebuggerPath';
		case 'cortex-debug':
		case 'mcu-debug':
			return 'gdbPath';
		default:
			return 'gdb';
	}
}

/** What to tell the user when the session's GDB has no Python interpreter. */
export function pythonMissingMessage(debugType: string): string {
	return (
		'This GDB has no Python interpreter, so GDR cannot run in it. ' +
		'xPack ships both variants: arm-none-eabi-gdb has no Python, arm-none-eabi-gdb-py3 does. ' +
		`Point the launch configuration's ${gdbPathKey(debugType)} at the -py3 binary.`
	);
}

/** Why *debugType* cannot host GDR, or `null` when it can. */
export function unsupportedReason(debugType: string): string | null {
	if (GDB_ADAPTERS[debugType] !== undefined) {
		return null;
	}
	return (
		`${debugType} does not expose a GDB console; GDR runs inside GDB's Python ` +
		`interpreter, so this adapter cannot host it. Use a GDB-backed adapter for ` +
		`the same target to get the panel.`
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
