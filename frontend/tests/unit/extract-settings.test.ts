import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  CHUNK_MAX_CHARS,
  CHUNK_OVERLAP,
  changedSettingsCount,
  DEFAULT_SETTINGS,
  normalizeCount,
  parsePageSpec,
  sameReadSettings,
  toParseOptions,
} from '../../src/features/extract/settings.ts';

test('an empty page list reads every page', () => {
  assert.deepEqual(parsePageSpec(''), { ok: true, spec: null });
  assert.deepEqual(parsePageSpec('   '), { ok: true, spec: null });
  assert.deepEqual(parsePageSpec(' , ,'), { ok: true, spec: null });
});

test('page lists are read the way people type them', () => {
  assert.deepEqual(parsePageSpec('1-3, 5'), { ok: true, spec: '1-3,5' });
  assert.deepEqual(parsePageSpec('1~3 5'), { ok: true, spec: '1-3,5' });
  assert.deepEqual(parsePageSpec('1 - 3'), { ok: true, spec: '1-3' });
  assert.deepEqual(parsePageSpec('2–4，7'), { ok: true, spec: '2-4,7' });
  assert.deepEqual(parsePageSpec('1,,2'), { ok: true, spec: '1,2' });
  assert.deepEqual(parsePageSpec('007'), { ok: true, spec: '7' });
});

test('a backwards range is turned around and a one-page range is shortened', () => {
  assert.deepEqual(parsePageSpec('5-3'), { ok: true, spec: '3-5' });
  assert.deepEqual(parsePageSpec('4-4'), { ok: true, spec: '4' });
});

test('parts that are not page numbers are reported', () => {
  assert.deepEqual(parsePageSpec('0'), { ok: false, token: '0' });
  assert.deepEqual(parsePageSpec('1, 셋째'), { ok: false, token: '셋째' });
  assert.deepEqual(parsePageSpec('3-'), { ok: false, token: '3-' });
  assert.deepEqual(parsePageSpec('1-2-3'), { ok: false, token: '1-2-3' });
  assert.deepEqual(parsePageSpec('0-2'), { ok: false, token: '0-2' });
  assert.deepEqual(parsePageSpec('100000'), { ok: false, token: '100000' });
});

test('typed numbers are clamped to the server range', () => {
  assert.equal(normalizeCount('', CHUNK_MAX_CHARS), 1500);
  assert.equal(normalizeCount('50', CHUNK_MAX_CHARS), 200);
  assert.equal(normalizeCount('1,500', CHUNK_MAX_CHARS), 1500);
  assert.equal(normalizeCount(' 3000 ', CHUNK_MAX_CHARS), 3000);
  assert.equal(normalizeCount('999999', CHUNK_MAX_CHARS), 20000);
  assert.equal(normalizeCount('열', CHUNK_MAX_CHARS), 1500);
  assert.equal(normalizeCount('1.5', CHUNK_MAX_CHARS), 1500);
  assert.equal(normalizeCount('-5', CHUNK_OVERLAP), 0);
  assert.equal(normalizeCount('0', CHUNK_OVERLAP), 0);
});

test('default settings become the server defaults', () => {
  assert.deepEqual(toParseOptions(DEFAULT_SETTINGS), {
    ok: true,
    options: {
      pages: null,
      layout_detector: 'none',
      include_comments: true,
      extract_crops: true,
      include_chunks: true,
      chunk_max_chars: 1500,
      chunk_overlap: 150,
    },
  });
});

test('changed settings are sent as typed, cleaned up', () => {
  const result = toParseOptions({
    pages: '2~4, 9',
    findLayout: true,
    includeComments: false,
    extractCrops: false,
    includeChunks: true,
    chunkMaxChars: '800',
    chunkOverlap: '0',
  });
  assert.deepEqual(result, {
    ok: true,
    options: {
      pages: '2-4,9',
      layout_detector: 'heuristic',
      include_comments: false,
      extract_crops: false,
      include_chunks: true,
      chunk_max_chars: 800,
      chunk_overlap: 0,
    },
  });
});

test('a page list that cannot be read stops the options', () => {
  assert.deepEqual(toParseOptions({ ...DEFAULT_SETTINGS, pages: '1-2, 둘' }), { ok: false, token: '둘' });
});

test('settings that read the same way compare equal', () => {
  assert.ok(sameReadSettings(DEFAULT_SETTINGS, { ...DEFAULT_SETTINGS }));
  assert.ok(sameReadSettings({ ...DEFAULT_SETTINGS, pages: '1-3' }, { ...DEFAULT_SETTINGS, pages: '1~3' }));
  assert.ok(sameReadSettings({ ...DEFAULT_SETTINGS, chunkMaxChars: '1,500' }, DEFAULT_SETTINGS));
  assert.ok(!sameReadSettings({ ...DEFAULT_SETTINGS, pages: '2' }, DEFAULT_SETTINGS));
  assert.ok(!sameReadSettings({ ...DEFAULT_SETTINGS, extractCrops: false }, DEFAULT_SETTINGS));
  assert.ok(!sameReadSettings({ ...DEFAULT_SETTINGS, chunkOverlap: '10' }, DEFAULT_SETTINGS));
});

test('chunk sizes do not matter when no chunks are made', () => {
  const off = { ...DEFAULT_SETTINGS, includeChunks: false };
  assert.ok(sameReadSettings(off, { ...off, chunkMaxChars: '900', chunkOverlap: '0' }));
});

test('the 세부 설정 toggle counts changed options', () => {
  assert.equal(changedSettingsCount(DEFAULT_SETTINGS), 0);
  assert.equal(changedSettingsCount({ ...DEFAULT_SETTINGS, chunkMaxChars: '1,500' }), 0);
  assert.equal(changedSettingsCount({ ...DEFAULT_SETTINGS, pages: '1-2', findLayout: true }), 2);
  assert.equal(changedSettingsCount({ ...DEFAULT_SETTINGS, pages: '첫째' }), 1);
});
