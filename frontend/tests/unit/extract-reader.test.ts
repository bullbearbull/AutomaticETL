import assert from 'node:assert/strict';
import { test } from 'node:test';
import { normalizeParseResponse, type ParseRequest } from '../../src/features/extract/api.ts';
import { readEntry, type ReadDeps } from '../../src/features/extract/reader.ts';
import { DEFAULT_SETTINGS } from '../../src/features/extract/settings.ts';
import { abortedError, errorFromResponse } from '../../src/lib/errors.ts';
import { sampleRaw } from './extract-sample.ts';

const RESULT = normalizeParseResponse(sampleRaw());
const TARGET = { file: new Blob(['PK']), name: '문서관리_규정_예시.docx' };

function deps(overrides: Partial<ReadDeps> = {}): ReadDeps & { requests: ParseRequest[] } {
  const requests: ParseRequest[] = [];
  return {
    requests,
    readBase64: async () => 'UEs=',
    parse: async (request) => {
      requests.push(request);
      return RESULT;
    },
    ...overrides,
  };
}

test('a file is read with the current settings', async () => {
  const fake = deps();
  const outcome = await readEntry(TARGET, { ...DEFAULT_SETTINGS, pages: '2 ~ 3' }, new AbortController().signal, fake);
  assert.deepEqual(outcome, { type: 'succeed', result: RESULT });
  assert.equal(fake.requests.length, 1);
  assert.equal(fake.requests[0]?.filename, '문서관리_규정_예시.docx');
  assert.equal(fake.requests[0]?.content_base64, 'UEs=');
  assert.equal(fake.requests[0]?.options.pages, '2-3');
});

test('a page list that cannot be read stops before anything is sent', async () => {
  const fake = deps({
    readBase64: async () => {
      throw new Error('should not read');
    },
  });
  const outcome = await readEntry(TARGET, { ...DEFAULT_SETTINGS, pages: '1, 둘' }, new AbortController().signal, fake);
  assert.equal(outcome.type, 'fail');
  assert.equal(outcome.type === 'fail' && outcome.error.what, '고른 쪽 번호를 알아보지 못했어요');
  assert.equal(fake.requests.length, 0);
});

test('a file the browser cannot open is explained', async () => {
  const fake = deps({
    readBase64: async () => {
      throw new DOMException('The file could not be read.', 'NotReadableError');
    },
  });
  const outcome = await readEntry(TARGET, DEFAULT_SETTINGS, new AbortController().signal, fake);
  assert.equal(outcome.type === 'fail' && outcome.error.what, '파일을 열지 못했어요');
});

test('server errors are refined for this tab', async () => {
  const fake = deps({
    parse: async () => {
      throw errorFromResponse(422, '선택한 페이지 번호가 문서 범위(1~2)를 벗어났다: [5]');
    },
  });
  const outcome = await readEntry(TARGET, { ...DEFAULT_SETTINGS, pages: '5' }, new AbortController().signal, fake);
  assert.equal(outcome.type === 'fail' && outcome.error.why, '이 파일은 2쪽까지 있어요.');
});

test('a cancelled read reports nothing to show', async () => {
  const controller = new AbortController();
  const fake = deps({
    parse: async () => {
      controller.abort();
      throw abortedError();
    },
  });
  assert.deepEqual(await readEntry(TARGET, DEFAULT_SETTINGS, controller.signal, fake), { type: 'aborted' });

  const early = new AbortController();
  const reading = deps({
    readBase64: async () => {
      early.abort();
      return 'UEs=';
    },
  });
  assert.deepEqual(await readEntry(TARGET, DEFAULT_SETTINGS, early.signal, reading), { type: 'aborted' });
  assert.equal(reading.requests.length, 0);
});
