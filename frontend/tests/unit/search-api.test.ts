import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  buildDocIndex,
  normalizeBuildResponse,
  normalizeQueryResponse,
  normalizeSectionResponse,
  normalizeTableSqlResponse,
  queryDocIndex,
} from '../../src/features/search/api.ts';
import { AppError } from '../../src/lib/errors.ts';

test('normalizeBuildResponse parses valid /api/docindex/build payload', () => {
  const res = normalizeBuildResponse({
    index_id: 'idx-1',
    download_token: 'tok-1',
    elapsed_seconds: 0.42,
    summary: {
      documents_indexed: 1,
      documents_skipped: 0,
      sections_indexed: 2,
      tables_indexed: 1,
      elapsed_seconds: 0.4,
      warnings: [],
    },
    documents: [
      {
        doc_id: 'doc-1',
        source_path: 'docs/policy.pdf',
        format: 'pdf',
        sha256: 'abc',
        title: '보안 규정',
        page_count: 4,
        section_count: 2,
        table_count: 1,
      },
    ],
    sections: [
      {
        section_id: 'doc-1:s0001',
        doc_id: 'doc-1',
        source_path: 'docs/policy.pdf',
        heading_level: 1,
        title: '보관 기간',
        section_path: ['제1장', '보관 기간'],
        page_start: 2,
        page_end: 3,
        char_count: 120,
        table_ids: ['doc-1:p2_t1'],
      },
    ],
    tables: [
      {
        table_uid: 'doc-1:p2_t1',
        doc_id: 'doc-1',
        source_path: 'docs/policy.pdf',
        table_id: 'p2_t1',
        page: 2,
        caption: '보관 연한 표',
        row_count: 3,
        col_count: 2,
        headers: ['항목', '기간'],
      },
    ],
  });

  assert.equal(res.indexId, 'idx-1');
  assert.equal(res.documents[0]?.pageCount, 4);
  assert.deepEqual(res.sections[0]?.sectionPath, ['제1장', '보관 기간']);
  assert.equal(res.tables[0]?.tableUid, 'doc-1:p2_t1');
});

test('normalizers reject malformed payloads with AppError', () => {
  assert.throws(
    () => normalizeBuildResponse({ documents: [] }),
    (err: unknown) => err instanceof AppError && err.kind === 'server',
  );
  assert.throws(
    () => normalizeQueryResponse({ query: '보관' }),
    (err: unknown) => err instanceof AppError && err.kind === 'server',
  );
  assert.throws(
    () => normalizeSectionResponse({ full_text: '본문' }),
    (err: unknown) => err instanceof AppError && err.kind === 'server',
  );
  assert.throws(
    () => normalizeTableSqlResponse({ columns: ['a'] }),
    (err: unknown) => err instanceof AppError && err.kind === 'server',
  );
});

test('normalizeQueryResponse parses hits and table_hits', () => {
  const pack = normalizeQueryResponse({
    query: '보관 기간',
    mode: 'fts5_bm25',
    floor_calibrated: true,
    elapsed_ms: 4.5,
    total_candidates: 2,
    hits: [
      {
        section_id: 'doc-1:s0001',
        doc_id: 'doc-1',
        source_path: 'policy.docx',
        title: '제3조 보관 기간',
        section_path: ['제3조 보관 기간'],
        page_start: 1,
        page_end: 2,
        full_text: '보관 기간은 5년이다.',
        bm25_score: 2.1,
        outline_boost: 1.0,
        rerank_score: null,
        final_score: 0.76,
        matched_terms: ['보관', '기간'],
        table_ids: [],
      },
    ],
    table_hits: [],
  });
  assert.equal(pack.hits.length, 1);
  assert.equal(pack.hits[0]?.finalScore, 0.76);
  assert.deepEqual(pack.hits[0]?.matchedTerms, ['보관', '기간']);
});

test('buildDocIndex and queryDocIndex post JSON and map expired 404 responses', async () => {
  let calledUrl = '';
  const fetchMock: typeof fetch = async (input) => {
    calledUrl = String(input);
    return new Response(
      JSON.stringify({ error: '문서 인덱스를 찾을 수 없다(만료되었거나 잘못된 index_id).' }),
      { status: 404, headers: { 'Content-Type': 'application/json' } },
    );
  };

  await assert.rejects(
    () => queryDocIndex({ index_id: 'gone', query: '보관', top_k: 5 }, { fetchImpl: fetchMock }),
    (err: unknown) => err instanceof AppError && err.kind === 'expired',
  );
  assert.equal(calledUrl, '/api/docindex/query');

  const okFetch: typeof fetch = async () =>
    new Response(
      JSON.stringify({
        index_id: 'idx-ok',
        elapsed_seconds: 0.1,
        summary: { documents_indexed: 0, documents_skipped: 0, sections_indexed: 0, tables_indexed: 0, elapsed_seconds: 0.1, warnings: [] },
        documents: [],
        sections: [],
        tables: [],
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    );
  const built = await buildDocIndex(
    { files: [{ filename: 'a.pdf', content_base64: 'QQ==' }] },
    { fetchImpl: okFetch },
  );
  assert.equal(built.indexId, 'idx-ok');
});
