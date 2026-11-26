import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  AppError,
  errorFromResponse,
  extensionFromServerMessage,
  fileTooLargeError,
  networkError,
  toAppError,
  unsupportedFileError,
} from '../../src/lib/errors.ts';

test('unsupported format: .xls gets the save-as-xlsx instructions', () => {
  const raw = "지원하지 않는 파일 형식이다: '.xls' (지원: .doc, .docx, .hwp, .hwpx, .pdf, .ppt, .pptx, .xlsx)";
  const error = errorFromResponse(400, raw);
  assert.equal(error.kind, 'unsupportedFile');
  assert.equal(error.what, '이 파일은 읽을 수 없어요');
  assert.equal(error.why, '구형 엑셀(.xls) 파일은 아직 지원하지 않아요.');
  assert.ok(error.how.includes('Excel 통합 문서(*.xlsx)'));
  assert.ok(error.detail?.includes(raw));
  assert.equal(error.status, 400);
});

test('unsupported format: other extensions get a generic hint', () => {
  const error = errorFromResponse(400, "지원하지 않는 테이블 파일 형식이다: '.ODS' (지원: .csv)");
  assert.equal(error.kind, 'unsupportedFile');
  assert.equal(error.why, '.ods 파일은 아직 지원하지 않아요.');
  assert.equal(extensionFromServerMessage("형식이다: '(없음)'"), '');
  const listed = unsupportedFileError('.txt', { supported: 'PDF·한글' });
  assert.equal(listed.why, '.txt 파일은 아직 지원하지 않아요. 읽을 수 있는 형식은 PDF·한글예요.');
  assert.equal(unsupportedFileError('').why, '확장자가 없는 파일은 읽을 수 없어요.');
});

test('size errors map by status or message prefix', () => {
  assert.equal(errorFromResponse(413, '요청 본문 크기가 허용 상한(50MB)을 초과했다.').kind, 'tooLarge');
  assert.equal(errorFromResponse(413, '').kind, 'tooLarge');
  assert.equal(errorFromResponse(400, '업로드 파일 크기가 50MB 상한을 초과했다.').kind, 'tooLarge');
  const local = fileTooLargeError('63MB', '50MB');
  assert.equal(local.why, '이 파일은 63MB이고, 한 번에 넣을 수 있는 크기는 50MB까지예요.');
});

test('403 from the Host or Origin checks', () => {
  for (const raw of ['허용하지 않는 Host 헤더다.', '다른 출처에서 온 요청은 허용하지 않는다.']) {
    const error = errorFromResponse(403, raw);
    assert.equal(error.kind, 'forbidden');
    assert.ok(error.detail?.endsWith(raw));
  }
});

test('expired results, unreadable files, 5xx and other 4xx', () => {
  assert.equal(errorFromResponse(404, '요청한 번들을 찾을 수 없다.').kind, 'expired');
  assert.equal(errorFromResponse(404, '분석 세션을 찾을 수 없습니다. 파일을 다시 올려 주세요.').kind, 'expired');
  assert.equal(errorFromResponse(404, '알 수 없는 경로다.').kind, 'request');
  assert.equal(errorFromResponse(422, '문서를 열 수 없다').kind, 'unreadable');
  assert.equal(errorFromResponse(500, '내부 오류').kind, 'server');
  assert.equal(errorFromResponse(502, '').kind, 'server');
  assert.equal(errorFromResponse(415, '요청 본문은 application/json 형식이어야 한다.').kind, 'request');
  assert.equal(errorFromResponse(500, '').detail, 'HTTP 500');
});

test('every mapped error has all three parts in 해요체', () => {
  const samples = [
    errorFromResponse(400, "지원하지 않는 파일 형식이다: '.xls'"),
    errorFromResponse(403, ''),
    errorFromResponse(404, '요청한 번들을 찾을 수 없다.'),
    errorFromResponse(413, ''),
    errorFromResponse(422, ''),
    errorFromResponse(500, ''),
    errorFromResponse(400, ''),
    networkError(new TypeError('Failed to fetch')),
  ];
  for (const error of samples) {
    assert.ok(error instanceof AppError);
    assert.ok(error.what && error.why && error.how, error.kind);
    for (const part of [error.what, error.why, error.how]) {
      assert.ok(!/(합니다|한다|이다|했다)\.?$/.test(part), `${error.kind}: ${part}`);
    }
  }
});

test('toAppError: network TypeError, abort, AppError and unknown values', () => {
  assert.equal(toAppError(new TypeError('Failed to fetch')).kind, 'network');
  assert.equal(toAppError(new DOMException('Aborted', 'AbortError')).kind, 'aborted');
  const original = errorFromResponse(500, 'x');
  assert.equal(toAppError(original), original);
  const unknown = toAppError('문자열');
  assert.equal(unknown.kind, 'server');
  assert.equal(unknown.detail, '문자열');
});
