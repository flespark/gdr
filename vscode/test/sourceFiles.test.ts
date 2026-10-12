/**
 * Unit tests for the source-file resolver: the fullname first, then relative
 * and `..` paths, absolute paths that are absent, the longest workspace suffix,
 * and ties that must not be guessed.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { normalizeSourcePath, resolveSourceFile, sharedSuffixLength } from '../src/sourceFiles.ts';

const HERE = new Set(['/ws/app/applications/main.c', '/ws/app/src/shell.c', '/ws/lib/src/shell.c']);
const exists = (path: string): boolean => HERE.has(path);

test('a fullname that exists here is used as it is', () => {
	assert.deepEqual(resolveSourceFile('applications/main.c', '/ws/app/applications/main.c', [], exists), {
		kind: 'found',
		path: '/ws/app/applications/main.c'
	});
});

test('a relative name with no existing fullname matches its workspace tail', () => {
	assert.deepEqual(
		resolveSourceFile('applications/main.c', '/build/bsp/applications/main.c', ['/ws/app/applications/main.c'], exists),
		{ kind: 'found', path: '/ws/app/applications/main.c' }
	);
});

test('a name with .. segments is folded before it is matched', () => {
	assert.deepEqual(
		resolveSourceFile('../src/shell.c', '/build/missing.c', ['/ws/app/src/shell.c'], exists),
		{ kind: 'found', path: '/ws/app/src/shell.c' }
	);
	assert.deepEqual(normalizeSourcePath('a/./b/../c.c'), ['a', 'c.c']);
});

test('an absolute path that is absent here falls through to the workspace suffix', () => {
	assert.deepEqual(
		resolveSourceFile('/opt/ci/build/src/shell.c', '/opt/ci/build/src/shell.c', ['/ws/app/src/shell.c'], exists),
		{ kind: 'found', path: '/ws/app/src/shell.c' }
	);
});

test('the longest shared tail wins over a shorter one', () => {
	const candidates = ['/ws/app/src/shell.c', '/ws/lib/src/shell.c'];
	assert.deepEqual(resolveSourceFile('app/src/shell.c', '/gone.c', candidates, () => false), {
		kind: 'found',
		path: '/ws/app/src/shell.c'
	});
});

test('equally good candidates are offered, never silently picked', () => {
	const candidates = ['/ws/app/src/shell.c', '/ws/lib/src/shell.c'];
	const resolved = resolveSourceFile('src/shell.c', '/gone.c', candidates, () => false);
	assert.equal(resolved.kind, 'ambiguous');
	assert.deepEqual(resolved.kind === 'ambiguous' ? resolved.paths.sort() : [], candidates.sort());
});

test('no shared segment means no file, so the caller falls back to the memory view', () => {
	assert.deepEqual(resolveSourceFile('drivers/uart.c', '/gone.c', ['/ws/app/src/shell.c'], () => false), {
		kind: 'missing'
	});
	assert.deepEqual(resolveSourceFile('', '', [], () => false), { kind: 'missing' });
});

test('backslashes and a drive letter normalize to the same key as forward slashes', () => {
	assert.deepEqual(normalizeSourcePath('C:\\src\\shell.c'), ['C:', 'src', 'shell.c']);
	// The drive letter has no counterpart in the POSIX path, so only the three
	// shared segments count: ws, src and x.c.
	assert.equal(sharedSuffixLength(normalizeSourcePath('C:\\ws\\src\\x.c'), normalizeSourcePath('/ws/src/x.c')), 3);
});

test('a climb above the root is clamped rather than escaping the workspace', () => {
	assert.deepEqual(normalizeSourcePath('/a/../../b.c'), ['b.c']);
});
