import assert from 'node:assert/strict';
import { test } from 'node:test';
import { normalizeParseResponse } from '../../src/features/extract/api.ts';
import { checkExtractFile } from '../../src/features/extract/formats.ts';
import { DEFAULT_SETTINGS } from '../../src/features/extract/settings.ts';
import {
  entryMeta,
  extractReducer,
  initialState,
  isZipExpired,
  nextQueued,
  selectedEntry,
  SERVER_KEEPS_ZIPS,
  statusFor,
  stepFor,
  type ExtractAction,
  type ExtractState,
} from '../../src/features/extract/state.ts';
import { AppError } from '../../src/lib/errors.ts';
import { sampleRaw } from './extract-sample.ts';

const RESULT = normalizeParseResponse(sampleRaw());

function file(id: string, name: string, size = 100) {
  return { id, file: new Blob(['x']), name, size, error: checkExtractFile({ name, size }) };
}

function run(actions: readonly ExtractAction[], state: ExtractState = initialState): ExtractState {
  return actions.reduce(extractReducer, state);
}

function serverError(): AppError {
  return new AppError({ kind: 'server', what: '처리하는 중에 문제가 생겼어요', why: '-', how: '-' });
}

test('added files wait in order and the first one is shown', () => {
  const state = run([{ type: 'add', files: [file('a', '가.pdf'), file('b', '나.docx')] }]);
  assert.deepEqual(
    state.entries.map((entry) => [entry.id, entry.status]),
    [
      ['a', 'queued'],
      ['b', 'queued'],
    ],
  );
  assert.equal(state.selectedId, 'a');
  assert.equal(nextQueued(state)?.id, 'a');
});

test('a file the browser check stops is an error right away', () => {
  const state = run([{ type: 'add', files: [file('x', '거래처_목록.xls')] }]);
  assert.equal(state.entries[0]?.status, 'error');
  assert.equal(state.entries[0]?.error?.kind, 'unsupportedFile');
  assert.equal(nextQueued(state), null);
  assert.equal(entryMeta(state.entries[0]!), '읽을 수 없어요');
});

test('adding more files keeps a shown result but replaces a shown error', () => {
  const done = run([
    { type: 'add', files: [file('a', '가.pdf')] },
    { type: 'start', id: 'a', settings: DEFAULT_SETTINGS, now: 1000 },
    { type: 'succeed', id: 'a', result: RESULT, now: 3000 },
    { type: 'add', files: [file('b', '나.pdf')] },
  ]);
  assert.equal(done.selectedId, 'a');

  const failed = run([
    { type: 'add', files: [file('x', '거래처_목록.xls')] },
    { type: 'add', files: [file('b', '나.pdf')] },
  ]);
  assert.equal(failed.selectedId, 'b');
});

test('a read moves from reading to done and keeps its settings', () => {
  const settings = { ...DEFAULT_SETTINGS, pages: '1' };
  const reading = run([
    { type: 'add', files: [file('a', '가.pdf')] },
    { type: 'start', id: 'a', settings, now: 1000 },
  ]);
  assert.equal(reading.entries[0]?.status, 'reading');
  assert.equal(entryMeta(reading.entries[0]!), '읽는 중');
  const done = run([{ type: 'succeed', id: 'a', result: RESULT, now: 4000 }], reading);
  const entry = done.entries[0]!;
  assert.equal(entry.status, 'done');
  assert.equal(entry.settings, settings);
  assert.equal(entry.startedAt, 1000);
  assert.equal(entry.endedAt, 4000);
  assert.equal(entryMeta(entry), '2쪽 · 표 1개');
});

test('a failed read keeps the error until it is read again', () => {
  const failed = run([
    { type: 'add', files: [file('a', '가.pdf')] },
    { type: 'start', id: 'a', settings: DEFAULT_SETTINGS, now: 1 },
    { type: 'fail', id: 'a', error: serverError(), now: 2 },
  ]);
  assert.equal(failed.entries[0]?.status, 'error');
  assert.equal(entryMeta(failed.entries[0]!), '읽지 못했어요');
  const retried = run([{ type: 'retry', id: 'a' }], failed);
  assert.equal(retried.entries[0]?.status, 'queued');
  assert.equal(retried.entries[0]?.error, null);
});

test('a file that is being read cannot be queued twice', () => {
  const reading = run([
    { type: 'add', files: [file('a', '가.pdf')] },
    { type: 'start', id: 'a', settings: DEFAULT_SETTINGS, now: 1 },
    { type: 'retry', id: 'a' },
  ]);
  assert.equal(reading.entries[0]?.status, 'reading');
});

test('removing the shown file shows the next one, or the one before at the end', () => {
  const three = run([{ type: 'add', files: [file('a', '가.pdf'), file('b', '나.pdf'), file('c', '다.pdf')] }]);
  const middle = run([{ type: 'select', id: 'b' }, { type: 'remove', id: 'b' }], three);
  assert.equal(middle.selectedId, 'c');
  const last = run([{ type: 'select', id: 'c' }, { type: 'remove', id: 'c' }], three);
  assert.equal(last.selectedId, 'b');
  const other = run([{ type: 'remove', id: 'c' }], three);
  assert.equal(other.selectedId, 'a');
  const empty = run([{ type: 'remove', id: 'a' }], run([{ type: 'add', files: [file('a', '가.pdf')] }]));
  assert.equal(empty.selectedId, null);
  assert.equal(selectedEntry(empty), null);
});

test('selecting an unknown file changes nothing', () => {
  const state = run([{ type: 'add', files: [file('a', '가.pdf')] }]);
  assert.equal(run([{ type: 'select', id: 'zzz' }], state), state);
});

test('the status line follows the files', () => {
  assert.deepEqual(statusFor(initialState), {
    message: '준비됐어요 · 파일을 넣으면 바로 읽기 시작해요',
    tone: 'idle',
    startedAt: null,
    endedAt: null,
  });

  const one = run([
    { type: 'add', files: [file('a', '보고서.pdf')] },
    { type: 'start', id: 'a', settings: DEFAULT_SETTINGS, now: 1000 },
  ]);
  assert.deepEqual(statusFor(one), {
    message: '“보고서.pdf”를 읽는 중이에요',
    tone: 'busy',
    startedAt: 1000,
    endedAt: null,
  });

  const two = run([{ type: 'add', files: [file('b', '규정.docx')] }, { type: 'succeed', id: 'a', result: RESULT, now: 2000 }, { type: 'start', id: 'b', settings: DEFAULT_SETTINGS, now: 2500 }], one);
  assert.equal(statusFor(two).message, '“규정.docx”를 읽는 중이에요 · 파일 2개 중 2번째');
  assert.ok(!statusFor(two).message.includes('단계'));
});

test('a file about to start already reads as busy', () => {
  const queued = run([{ type: 'add', files: [file('a', '보고서.pdf')] }]);
  assert.equal(statusFor(queued).tone, 'busy');
  assert.equal(statusFor(queued).startedAt, null);
});

test('finished reads are summed up', () => {
  const single = run([
    { type: 'add', files: [file('a', '보고서.pdf')] },
    { type: 'start', id: 'a', settings: DEFAULT_SETTINGS, now: 1000 },
    { type: 'succeed', id: 'a', result: RESULT, now: 4000 },
  ]);
  assert.deepEqual(statusFor(single), {
    message: '“보고서.pdf”를 다 읽었어요 · 표 1개 · 그림·차트 1개',
    tone: 'done',
    startedAt: 1000,
    endedAt: 4000,
  });

  const xls = run([{ type: 'add', files: [file('x', '거래처_목록.xls')] }]);
  assert.equal(statusFor(xls).message, '“거래처_목록.xls”를 읽지 못했어요');
  assert.equal(statusFor(xls).tone, 'error');

  const mixed = run([{ type: 'add', files: [file('x', '거래처_목록.xls')] }], single);
  assert.equal(statusFor(mixed).message, '파일 2개 중 1개를 다 읽었어요 · 1개는 읽지 못했어요');
  assert.equal(statusFor(mixed).tone, 'warning');

  const allFailed = run([{ type: 'add', files: [file('x', '가.xls'), file('y', '나.txt')] }]);
  assert.equal(statusFor(allFailed).message, '파일 2개를 모두 읽지 못했어요');

  const allDone = run([
    { type: 'add', files: [file('b', '나.pdf')] },
    { type: 'start', id: 'b', settings: DEFAULT_SETTINGS, now: 5000 },
    { type: 'succeed', id: 'b', result: RESULT, now: 6000 },
  ], single);
  assert.equal(statusFor(allDone).message, '파일 2개를 다 읽었어요');
});

test('the step moves to 2 with a result and to 3 once it is copied or saved', () => {
  const start = run([{ type: 'add', files: [file('a', '가.pdf')] }]);
  assert.equal(stepFor(initialState), 1);
  assert.equal(stepFor(start), 1);
  const done = run([
    { type: 'start', id: 'a', settings: DEFAULT_SETTINGS, now: 1 },
    { type: 'succeed', id: 'a', result: RESULT, now: 2 },
  ], start);
  assert.equal(stepFor(done), 2);
  const exported = run([{ type: 'exported', id: 'a' }], done);
  assert.equal(stepFor(exported), 3);
  const other = run([{ type: 'add', files: [file('b', '나.pdf')] }, { type: 'select', id: 'b' }], exported);
  assert.equal(stepFor(other), 2);
  const again = run([{ type: 'retry', id: 'a' }, { type: 'select', id: 'a' }, { type: 'start', id: 'a', settings: DEFAULT_SETTINGS, now: 3 }], exported);
  assert.equal(again.entries[0]?.exported, false);
});

test(`a ZIP is gone once ${SERVER_KEEPS_ZIPS} newer ZIPs were made, unless it was fetched since`, () => {
  let state = run([
    { type: 'add', files: [file('a', '가.pdf')] },
    { type: 'start', id: 'a', settings: DEFAULT_SETTINGS, now: 1 },
    { type: 'succeed', id: 'a', result: RESULT, now: 2 },
  ]);
  for (let index = 0; index < SERVER_KEEPS_ZIPS - 1; index += 1) {
    const id = `n${index}`;
    state = run([
      { type: 'add', files: [file(id, `${index}.pdf`)] },
      { type: 'start', id, settings: DEFAULT_SETTINGS, now: 3 },
      { type: 'succeed', id, result: RESULT, now: 4 },
    ], state);
  }
  const first = () => state.entries[0]!;
  assert.equal(isZipExpired(state, first()), false);
  const fetched = run([{ type: 'zipFetched', id: 'a' }], state);
  state = run([
    { type: 'add', files: [file('last', '마지막.pdf')] },
    { type: 'start', id: 'last', settings: DEFAULT_SETTINGS, now: 5 },
    { type: 'succeed', id: 'last', result: RESULT, now: 6 },
  ], state);
  assert.equal(isZipExpired(state, first()), true);
  const refreshed = run([
    { type: 'add', files: [file('last', '마지막.pdf')] },
    { type: 'start', id: 'last', settings: DEFAULT_SETTINGS, now: 5 },
    { type: 'succeed', id: 'last', result: RESULT, now: 6 },
  ], fetched);
  assert.equal(isZipExpired(refreshed, refreshed.entries[0]!), false);
});

test('a ZIP the server no longer has is gone until the file is read again', () => {
  const done = run([
    { type: 'add', files: [file('a', '가.pdf'), file('b', '나.pdf')] },
    { type: 'start', id: 'a', settings: DEFAULT_SETTINGS, now: 1 },
    { type: 'succeed', id: 'a', result: RESULT, now: 2 },
    { type: 'start', id: 'b', settings: DEFAULT_SETTINGS, now: 3 },
    { type: 'succeed', id: 'b', result: RESULT, now: 4 },
  ]);
  assert.equal(isZipExpired(done, done.entries[0]!), false);
  const missing = run([{ type: 'zipMissing', id: 'a' }], done);
  assert.equal(isZipExpired(missing, missing.entries[0]!), true);
  assert.equal(isZipExpired(missing, missing.entries[1]!), false);
  assert.equal(run([{ type: 'zipMissing', id: 'a' }], missing).entries[0], missing.entries[0]);
  // Fetching cannot bring it back; a new read makes a new ZIP.
  const fetched = run([{ type: 'zipFetched', id: 'a' }], missing);
  assert.equal(isZipExpired(fetched, fetched.entries[0]!), true);
  const reread = run([
    { type: 'retry', id: 'a' },
    { type: 'start', id: 'a', settings: DEFAULT_SETTINGS, now: 5 },
    { type: 'succeed', id: 'a', result: RESULT, now: 6 },
  ], missing);
  assert.equal(isZipExpired(reread, reread.entries[0]!), false);
});
