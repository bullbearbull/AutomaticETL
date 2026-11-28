import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  normalizeBuildResponse,
  normalizeQueryResponse,
} from '../../src/features/search/api.ts';
import {
  canSearch,
  DEFAULT_SEARCH_SETTINGS,
  initialSearchState,
  isIndexStale,
  searchReducer,
  statusFor,
  stepFor,
} from '../../src/features/search/state.ts';
import { AppError } from '../../src/lib/errors.ts';

function fakeFile(name: string, size = 100): File {
  return new File([new Uint8Array(size)], name, { type: 'application/octet-stream' });
}

function sampleBuild() {
  return normalizeBuildResponse({
    index_id: 'idx-1',
    elapsed_seconds: 0.3,
    summary: {
      documents_indexed: 2,
      documents_skipped: 0,
      sections_indexed: 4,
      tables_indexed: 1,
      elapsed_seconds: 0.3,
      warnings: [],
    },
    documents: [
      { doc_id: 'd1', source_path: 'a.docx', format: 'docx', sha256: '1', title: '규정 A', page_count: 2, section_count: 2, table_count: 0 },
      { doc_id: 'd2', source_path: 'b.docx', format: 'docx', sha256: '2', title: '규정 B', page_count: 3, section_count: 2, table_count: 1 },
    ],
    sections: [],
    tables: [],
  });
}

function samplePack(query = '보관 기간') {
  return normalizeQueryResponse({
    query,
    mode: 'fts5_bm25',
    floor_calibrated: false,
    elapsed_ms: 3.1,
    total_candidates: 1,
    hits: [
      {
        section_id: 'd1:s0001',
        doc_id: 'd1',
        source_path: 'a.docx',
        title: '보관 기간',
        section_path: ['제1조', '보관 기간'],
        page_start: 1,
        page_end: 1,
        full_text: '보관 기간은 5년이다.',
        bm25_score: 2.0,
        outline_boost: 1.0,
        rerank_score: null,
        final_score: 0.72,
        matched_terms: ['보관', '기간'],
        table_ids: [],
      },
    ],
    table_hits: [],
  });
}

test('search step transitions and status messages follow copy rules (never mention 단계)', () => {
  let state = initialSearchState;
  assert.equal(DEFAULT_SEARCH_SETTINGS.topK, 5);
  assert.equal(stepFor(state), 1);
  assert.equal(canSearch(state), false);
  assert.equal(statusFor(state).tone, 'idle');
  assert.equal(statusFor(state).message.includes('단계'), false);

  state = searchReducer(state, {
    type: 'mergeFiles',
    items: [{ id: 'f1', file: fakeFile('a.docx'), name: 'a.docx', size: 100 }],
  });
  state = searchReducer(state, { type: 'startBuild', now: 100 });
  assert.equal(statusFor(state).tone, 'busy');
  assert.equal(stepFor(state), 1);

  state = searchReducer(state, {
    type: 'buildSucceeded',
    result: sampleBuild(),
    fileRevision: state.fileRevision,
    now: 200,
  });
  assert.equal(stepFor(state), 1);
  assert.equal(stepFor(state, 'outline'), 2);
  assert.equal(canSearch(state), true);
  assert.equal(isIndexStale(state), false);
  assert.equal(statusFor(state).tone, 'done');

  state = searchReducer(state, { type: 'startSearch', query: '보관 기간', now: 300 });
  assert.equal(stepFor(state), 2);
  assert.equal(statusFor(state).tone, 'busy');

  state = searchReducer(state, {
    type: 'searchSucceeded',
    result: samplePack(),
    fileRevision: state.fileRevision,
    now: 400,
  });
  assert.equal(state.selectedHitId, 'd1:s0001');
  assert.equal(stepFor(state), 2);
  assert.equal(statusFor(state).tone, 'done');

  state = searchReducer(state, { type: 'markCopied', fileRevision: state.fileRevision });
  assert.equal(stepFor(state), 3);

  state = searchReducer(state, { type: 'removeFile', id: 'f1' });
  assert.equal(stepFor(state), 1);
  assert.equal(state.index, null);
});

test('file changes mark old index stale, ignore late responses, and clear searching cleanly', () => {
  let state = searchReducer(initialSearchState, {
    type: 'mergeFiles',
    items: [{ id: 'f1', file: fakeFile('a.docx'), name: 'a.docx', size: 100 }],
  });
  state = searchReducer(state, {
    type: 'buildSucceeded',
    result: sampleBuild(),
    fileRevision: state.fileRevision,
    now: 50,
  });
  state = searchReducer(state, { type: 'startSearch', query: '보관 기간', now: 60 });
  const staleRev = state.fileRevision;

  // Adding a second file invalidates old search & marks index stale immediately.
  state = searchReducer(state, {
    type: 'mergeFiles',
    items: [{ id: 'f2', file: fakeFile('b.docx'), name: 'b.docx', size: 100 }],
  });
  assert.equal(isIndexStale(state), true);
  assert.equal(canSearch(state), false);
  assert.equal(state.searching, false);

  // Late response for older fileRevision is discarded without changing index or searchResult.
  state = searchReducer(state, {
    type: 'searchSucceeded',
    result: samplePack(),
    fileRevision: staleRev,
    now: 70,
  });
  assert.equal(state.searchResult, null);

  state = searchReducer(state, {
    type: 'buildSucceeded',
    result: sampleBuild(),
    fileRevision: staleRev,
    now: 80,
  });
  assert.equal(isIndexStale(state), true);

  // Rebuild failure on current revision keeps reference index visible while invalidating freshness so retry stays available.
  const err = new AppError({ kind: 'server', what: '실패', why: '원인', how: '방법' });
  state = searchReducer(state, {
    type: 'buildFailed',
    error: err,
    fileRevision: state.fileRevision,
    now: 90,
  });
  assert.equal(isIndexStale(state), true);
  assert.equal(canSearch(state), false);
  assert.equal(statusFor(state).tone, 'error');

  // Expired-index rebuild failure on already-indexed fileRevision also invalidates indexedFileRevision for retry.
  let expiredState = searchReducer(initialSearchState, {
    type: 'mergeFiles',
    items: [{ id: 'f1', file: fakeFile('a.docx'), name: 'a.docx', size: 100 }],
  });
  expiredState = searchReducer(expiredState, {
    type: 'buildSucceeded',
    result: sampleBuild(),
    fileRevision: expiredState.fileRevision,
    now: 100,
  });
  expiredState = searchReducer(expiredState, {
    type: 'buildFailed',
    error: err,
    fileRevision: expiredState.fileRevision,
    now: 110,
  });
  assert.equal(isIndexStale(expiredState), true);
  assert.equal(canSearch(expiredState), false);
});

test('cancelSearch and searchFailed always reset searching flag', () => {
  let state = searchReducer(initialSearchState, {
    type: 'mergeFiles',
    items: [{ id: 'f1', file: fakeFile('a.docx'), name: 'a.docx', size: 100 }],
  });
  state = searchReducer(state, {
    type: 'buildSucceeded',
    result: sampleBuild(),
    fileRevision: state.fileRevision,
    now: 10,
  });
  state = searchReducer(state, { type: 'startSearch', query: '보관', now: 20 });
  assert.equal(state.searching, true);
  state = searchReducer(state, { type: 'cancelSearch', now: 30 });
  assert.equal(state.searching, false);

  state = searchReducer(state, { type: 'startSearch', query: '보관', now: 40 });
  const err = new AppError({ kind: 'network', what: '끊김', why: '원인', how: '방법' });
  state = searchReducer(state, {
    type: 'searchFailed',
    error: err,
    fileRevision: state.fileRevision,
    now: 50,
  });
  assert.equal(state.searching, false);
  assert.equal(state.noticeError, err);
});

test('setDocFilter clears prior search results and setNoticeError preserves in-flight searching', () => {
  let state = searchReducer(initialSearchState, {
    type: 'mergeFiles',
    items: [{ id: 'f1', file: fakeFile('a.docx'), name: 'a.docx', size: 100 }],
  });
  state = searchReducer(state, {
    type: 'buildSucceeded',
    result: sampleBuild(),
    fileRevision: state.fileRevision,
    now: 10,
  });
  state = searchReducer(state, { type: 'startSearch', query: '보관 기간', now: 20 });
  const subErr = new AppError({ kind: 'request', what: '조회 실패', why: '원인', how: '방법' });
  state = searchReducer(state, {
    type: 'setNoticeError',
    error: subErr,
    fileRevision: state.fileRevision,
    now: 25,
  });
  assert.equal(state.searching, true);
  assert.equal(state.noticeError, subErr);

  state = searchReducer(state, {
    type: 'searchSucceeded',
    result: samplePack(),
    fileRevision: state.fileRevision,
    now: 30,
  });
  state = searchReducer(state, { type: 'markCopied', fileRevision: state.fileRevision });
  assert.equal(stepFor(state), 3);

  // Switching scope resets prior hits, notice, and copy state cleanly.
  state = searchReducer(state, { type: 'setDocFilter', docId: 'd2' });
  assert.equal(state.docFilterId, 'd2');
  assert.equal(state.searchResult, null);
  assert.equal(state.selectedHitId, null);
  assert.equal(state.noticeError, null);
  assert.equal(state.copied, false);
  assert.equal(state.searching, false);
});
