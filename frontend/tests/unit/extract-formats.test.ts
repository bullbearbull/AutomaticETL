import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  checkExtractFile,
  formatFromName,
  formatName,
  SUPPORTED_EXTENSIONS,
  unitForFormat,
} from '../../src/features/extract/formats.ts';
import { AppError } from '../../src/lib/errors.ts';

const MB = 1024 * 1024;

test('readable files pass', () => {
  for (const extension of SUPPORTED_EXTENSIONS) {
    assert.equal(checkExtractFile({ name: `예시${extension}`, size: 10 }), null);
  }
  assert.equal(checkExtractFile({ name: '예시.PDF', size: 50 * MB }), null);
});

test('an old Excel file gets the save-as-xlsx steps', () => {
  const error = checkExtractFile({ name: '거래처_목록.xls', size: 512 });
  assert.ok(error instanceof AppError);
  assert.equal(error.kind, 'unsupportedFile');
  assert.equal(error.what, '이 파일은 읽을 수 없어요');
  assert.match(error.why, /구형 엑셀\(\.xls\)/);
  assert.match(error.how, /Excel 통합 문서\(\*\.xlsx\)/);
  assert.match(error.detail ?? '', /확장자: \.xls/);
});

test('other formats list what can be read', () => {
  const error = checkExtractFile({ name: '메모.txt', size: 10 });
  assert.equal(error?.kind, 'unsupportedFile');
  assert.equal(
    error?.why,
    '.txt 파일은 아직 지원하지 않아요. 읽을 수 있는 형식은 PDF·한글(HWP·HWPX)·워드(DOC·DOCX)·파워포인트(PPT·PPTX)·엑셀(XLSX)예요.',
  );
  assert.match(checkExtractFile({ name: '확장자없음', size: 10 })?.why ?? '', /^확장자가 없는 파일은/);
});

test('files over 50MB are stopped before reading', () => {
  const error = checkExtractFile({ name: '큰_문서.pdf', size: 60 * MB });
  assert.equal(error?.kind, 'tooLarge');
  assert.equal(error?.why, '이 파일은 60MB이고, 한 번에 넣을 수 있는 크기는 50MB까지예요.');
  assert.match(error?.detail ?? '', /파일 크기: 62914560바이트/);
});

test('a file just over the limit does not read "50MB이고 … 50MB까지"', () => {
  const error = checkExtractFile({ name: '큰_문서.pdf', size: 50 * MB + 1 });
  assert.equal(error?.kind, 'tooLarge');
  assert.equal(error?.why, '이 파일은 50MB를 조금 넘어요. 한 번에 넣을 수 있는 크기는 50MB까지예요.');
});

test('an empty file is explained instead of being sent', () => {
  const error = checkExtractFile({ name: '빈_문서.pdf', size: 0 });
  assert.equal(error?.kind, 'unreadable');
  assert.equal(error?.what, '빈 파일이라 읽을 내용이 없어요');
});

test('formats name their unit and kind', () => {
  assert.equal(formatFromName('보고서.PPTX'), 'pptx');
  assert.equal(formatFromName('이름없음'), '');
  assert.equal(unitForFormat('pptx'), 'slide');
  assert.equal(unitForFormat('ppt'), 'slide');
  assert.equal(unitForFormat('xlsx'), 'sheet');
  assert.equal(unitForFormat('hwpx'), 'page');
  assert.equal(formatName('docx'), '워드 문서');
  assert.equal(formatName('hwp'), '한글 문서');
  assert.equal(formatName('xls'), 'XLS 파일');
  assert.equal(formatName(''), '확장자가 없는 파일');
});
