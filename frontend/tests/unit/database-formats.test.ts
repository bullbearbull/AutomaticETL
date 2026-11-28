import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  checkCombinedUploadSize,
  checkDatabaseFile,
  DATABASE_EXTENSIONS,
  formatKindLabel,
  MAX_TOTAL_UPLOAD_BYTES,
} from '../../src/features/database/formats.ts';
import { AppError } from '../../src/lib/errors.ts';

const MB = 1024 * 1024;

test('supported spreadsheet and delimited files pass', () => {
  for (const ext of DATABASE_EXTENSIONS) {
    assert.equal(checkDatabaseFile({ name: `sample${ext}`, size: 128 }), null);
  }
  assert.equal(checkDatabaseFile({ name: 'UPPER.CSV', size: MAX_TOTAL_UPLOAD_BYTES }), null);
});

test('legacy .xls files receive .xlsx conversion guidance', () => {
  const err = checkDatabaseFile({ name: 'old_sales.xls', size: 1024 });
  assert.ok(err instanceof AppError);
  assert.equal(err.kind, 'unsupportedFile');
  assert.match(err.why, /\.xls/);
  assert.match(err.how, /\.xlsx/);
});

test('unsupported formats and empty files fail cleanly', () => {
  const pdfErr = checkDatabaseFile({ name: 'report.pdf', size: 500 });
  assert.equal(pdfErr?.kind, 'unsupportedFile');
  assert.match(pdfErr?.why ?? '', /CSV\(\.csv\)/);

  const emptyErr = checkDatabaseFile({ name: 'empty.csv', size: 0 });
  assert.equal(emptyErr?.kind, 'unreadable');
  assert.match(emptyErr?.what ?? '', /비어 있어요/);
});

test('single and combined files over 50MB are rejected before reading', () => {
  const bigErr = checkDatabaseFile({ name: 'huge.csv', size: 51 * MB });
  assert.equal(bigErr?.kind, 'tooLarge');

  const combinedErr = checkCombinedUploadSize([
    { size: 30 * MB },
    { size: 25 * MB },
  ]);
  assert.equal(combinedErr?.kind, 'tooLarge');
  assert.equal(checkCombinedUploadSize([{ size: 20 * MB }, { size: 20 * MB }]), null);
});

test('formatKindLabel returns Korean labels', () => {
  assert.equal(formatKindLabel('csv'), 'CSV');
  assert.equal(formatKindLabel('tsv'), 'TSV');
  assert.equal(formatKindLabel('xlsx_sheet'), 'XLSX_SHEET');
  assert.equal(formatKindLabel('xlsx'), '엑셀');
});
