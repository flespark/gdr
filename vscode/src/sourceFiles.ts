/**
 * Find the workspace file a debug-info source path names.
 *
 * GDB reports two paths for a source line: the path it resolved (`fullname`,
 * which may be an absolute build directory that is absent on this machine) and
 * the path as the debug information recorded it (`name`, often relative to the
 * compile directory, or with `..` segments). The resolver tries them in order:
 *
 * 1. `fullname`, if that file exists here.
 * 2. `name` made absolute-free and normalized, then matched as a suffix against
 *    the workspace: the longest matching tail of its path wins.
 *
 * It is pure: the file-existence check and the workspace candidates are passed
 * in, so the node test runner can cover every case without a filesystem.
 */

/** The result: the one file to open, several to choose from, or nothing. */
export type SourceResolution =
	| { kind: 'found'; path: string }
	| { kind: 'ambiguous'; paths: string[] }
	| { kind: 'missing' };

/**
 * Fold `.` and `..` segments and unify separators to forward slashes.
 *
 * A path that climbs above its root keeps the climb out (`/a/../../b` is `/b`),
 * and a Windows drive letter stays in front. The result is a comparable key, not
 * a path to open: the caller opens the workspace file it matched.
 */
export function normalizeSourcePath(path: string): string[] {
	const unified = path.replace(/\\/g, '/');
	const drive = /^[A-Za-z]:/.test(unified) ? unified.slice(0, 2) : '';
	const body = drive !== '' ? unified.slice(2) : unified;
	const segments: string[] = [];
	for (const segment of body.split('/')) {
		if (segment === '' || segment === '.') {
			continue;
		}
		if (segment === '..') {
			segments.pop();
			continue;
		}
		segments.push(segment);
	}
	return drive !== '' ? [drive, ...segments] : segments;
}

/** The number of trailing path segments two paths share, compared case-sensitively. */
export function sharedSuffixLength(a: readonly string[], b: readonly string[]): number {
	let count = 0;
	while (count < a.length && count < b.length && a[a.length - 1 - count] === b[b.length - 1 - count]) {
		count += 1;
	}
	return count;
}

/**
 * Resolve one source reference to the workspace file it names.
 *
 * `candidates` are the workspace files whose basename matches the reference's
 * basename (the caller narrows with `findFiles` first). A candidate is accepted
 * when the reference's path tail matches its path tail for at least one segment.
 * Among candidates the longest shared tail wins; ties return every tying file.
 */
export function resolveSourceFile(
	name: string,
	fullname: string,
	candidates: readonly string[],
	exists: (path: string) => boolean
): SourceResolution {
	if (fullname !== '' && exists(fullname)) {
		return { kind: 'found', path: fullname };
	}
	const wanted = normalizeSourcePath(name);
	if (wanted.length === 0) {
		return { kind: 'missing' };
	}
	let best = 0;
	let tied: string[] = [];
	for (const candidate of candidates) {
		const shared = sharedSuffixLength(wanted, normalizeSourcePath(candidate));
		if (shared === 0) {
			continue;
		}
		if (shared > best) {
			best = shared;
			tied = [candidate];
		} else if (shared === best) {
			tied.push(candidate);
		}
	}
	if (tied.length === 0) {
		return { kind: 'missing' };
	}
	const unique = [...new Set(tied)];
	if (unique.length === 1) {
		return { kind: 'found', path: unique[0] };
	}
	return { kind: 'ambiguous', paths: unique };
}
