import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  errorAction,
  fileReadError,
  lastPageFromMessage,
  pageSpecError,
  refineReadError,
} from '../../src/features/extract/errors.ts';
import { AppError, errorFromResponse } from '../../src/lib/errors.ts';

test('page-range messages from each reader give the last page', () => {
  assert.equal(lastPageFromMessage('HTTP 422 · 선택한 페이지 번호가 문서 범위(1~2)를 벗어났다: [5]'), 2);
  assert.equal(lastPageFromMessage('HTTP 422 · 선택한 슬라이드 번호가 문서 범위(1~12)를 벗어났다: [13]'), 12);
  assert.equal(lastPageFromMessage('HTTP 422 · 선택한 시트 번호가 워크북 범위(1~3)를 벗어났다: [4]'), 3);
  assert.equal(lastPageFromMessage('HTTP 422 · 요청한 페이지 번호가 범위를 벗어났다(총 7페이지): [9]'), 7);
  assert.equal(lastPageFromMessage('HTTP 422 · 표를 읽지 못했다'), null);
});

test('a page past the end says which page is last and offers 다시 읽기', () => {
  const server = errorFromResponse(422, '선택한 페이지 번호가 문서 범위(1~2)를 벗어났다: [5]');
  assert.equal(server.kind, 'unreadable');
  const refined = refineReadError(server, 'page');
  assert.equal(refined.kind, 'request');
  assert.equal(refined.what, '고른 쪽이 이 파일에 없어요');
  assert.equal(refined.why, '이 파일은 2쪽까지 있어요.');
  assert.equal(refined.detail, server.detail);
  assert.equal(errorAction(refined.kind), 'retry');

  const slides = refineReadError(errorFromResponse(422, '선택한 슬라이드 번호가 문서 범위(1~12)를 벗어났다: [13]'), 'slide');
  assert.equal(slides.why, '이 파일의 슬라이드는 12번까지 있어요.');
  const sheets = refineReadError(errorFromResponse(422, '선택한 시트 번호가 워크북 범위(1~3)를 벗어났다: [4]'), 'sheet');
  assert.equal(sheets.why, '이 파일의 시트는 3번까지 있어요.');
});

test('a password-locked file says so', () => {
  const refined = refineReadError(
    errorFromResponse(422, '암호로 보호된 PDF 파일이다. 암호를 해제한 뒤 다시 입력해 달라.'),
    'page',
  );
  assert.equal(refined.what, '암호가 걸린 파일이라 읽지 못했어요');
  assert.equal(refined.kind, 'unreadable');
  assert.match(refined.detail ?? '', /암호로 보호된 PDF/);
});

test('other errors pass through unchanged', () => {
  const broken = errorFromResponse(422, '손상된 파일이다');
  assert.equal(refineReadError(broken, 'page'), broken);
  const server = errorFromResponse(500, '문서 파싱 중 내부 오류가 발생했다');
  assert.equal(refineReadError(server, 'page'), server);
});

test('file problems offer another file; problems on the way offer 다시 읽기', () => {
  assert.equal(errorAction('unsupportedFile'), 'pickOther');
  assert.equal(errorAction('tooLarge'), 'pickOther');
  assert.equal(errorAction('unreadable'), 'pickOther');
  for (const kind of ['network', 'forbidden', 'server', 'request', 'expired'] as const) {
    assert.equal(errorAction(kind), 'retry', kind);
  }
});

test('a page list part that is not a number is named in the reason', () => {
  const error = pageSpecError('셋째');
  assert.ok(error instanceof AppError);
  assert.equal(error.kind, 'request');
  assert.equal(error.why, '“셋째”는 쪽 번호로 읽을 수 없어요.');
});

test('a file the browser cannot open keeps the browser text under 자세히', () => {
  const error = fileReadError(new DOMException('A requested file could not be found.', 'NotFoundError'));
  assert.equal(error.what, '파일을 열지 못했어요');
  assert.equal(error.detail, 'NotFoundError: A requested file could not be found.');
});
