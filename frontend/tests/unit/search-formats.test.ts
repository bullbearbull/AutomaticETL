import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  baseFileName,
  checkCombinedUploadSize,
  checkSearchFile,
  formatPageRange,
  pageUnitFor,
} from '../../src/features/search/formats.ts';
import { AppError } from '../../src/lib/errors.ts';

function fakeFile(name: string, size = 512): { name: string; size: number } {
  return { name, size };
}

test('supported document formats pass validation', () => {
  for (const name of ['a.pdf', 'b.hwp', 'c.hwpx', 'd.docx', 'e.pptx', 'f.xlsx', 'g.doc', 'h.ppt']) {
    assert.equal(checkSearchFile(fakeFile(name)), null, name);
  }
});

test('legacy .xls files receive actionable .xlsx conversion guidance', () => {
  const err = checkSearchFile(fakeFile('old.xls'));
  assert.ok(err instanceof AppError);
  assert.equal(err.kind, 'unsupportedFile');
  assert.match(err.why, /\.xls/);
  assert.match(err.how, /\.xlsx/);
});

test('empty or oversized files and combined >50MB batches are rejected cleanly', () => {
  const empty = checkSearchFile(fakeFile('empty.pdf', 0));
  assert.ok(empty instanceof AppError);
  assert.equal(empty.kind, 'unreadable');

  const tooBig = checkSearchFile(fakeFile('huge.pdf', 51 * 1024 * 1024));
  assert.ok(tooBig instanceof AppError);
  assert.equal(tooBig.kind, 'tooLarge');

  const combined = checkCombinedUploadSize([
    fakeFile('a.pdf', 30 * 1024 * 1024),
    fakeFile('b.pdf', 25 * 1024 * 1024),
  ]);
  assert.ok(combined instanceof AppError);
  assert.equal(combined.kind, 'tooLarge');
});

test('formatPageRange, pageUnitFor, and baseFileName format citations naturally', () => {
  assert.equal(pageUnitFor('pdf'), '쪽');
  assert.equal(pageUnitFor('deck.pptx'), '슬라이드');
  assert.equal(pageUnitFor('.xlsx'), '시트');
  assert.equal(formatPageRange(3, 3, '쪽'), '3쪽');
  assert.equal(formatPageRange(3, 5, '쪽'), '3–5쪽');
  assert.equal(formatPageRange(2, 4, '슬라이드'), '2–4슬라이드');
  assert.equal(baseFileName('C:\\docs\\규정.hwpx'), '규정.hwpx');
  assert.equal(baseFileName('/tmp/docs/규정.pdf'), '규정.pdf');
});
