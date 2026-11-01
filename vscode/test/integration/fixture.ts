/** Repository-owned fixtures and registered host tools for integration tests. */
import { existsSync, mkdirSync } from 'node:fs';
import { arch, platform } from 'node:os';
import * as path from 'node:path';

export const REPO_ROOT = path.resolve(process.cwd(), '..');
export const CACHE = path.join(REPO_ROOT, 'ci', '.cache');
const CPU = arch() === 'x64' ? 'x86_64' : arch() === 'arm64' && platform() === 'linux' ? 'aarch64' : arch();
const TOOLS = path.join(CACHE, 'tools', `${platform()}-${CPU}`);

export function cachedTool(names: string[]): string {
	for (const name of names) {
		const candidate = path.join(TOOLS, 'bin', name);
		if (existsSync(candidate)) {
			return candidate;
		}
	}
	return names[names.length - 1];
}

export function temporaryDirectory(): string {
	const directory = path.join(CACHE, 'tmp', 'vscode');
	mkdirSync(directory, { recursive: true });
	return directory;
}
