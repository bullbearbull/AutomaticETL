import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  downloadPath,
  normalizeParseResponse,
  parseDocument,
  safeImageDataUrl,
  type ParseRequest,
} from '../../src/features/extract/api.ts';
import { AppError } from '../../src/lib/errors.ts';
import { sampleRaw, TINY_PNG } from './extract-sample.ts';

test('the server answer becomes a ParseResult', () => {
  const result = normalizeParseResponse(sampleRaw());
  assert.equal(result.bundleId, '6dec1208d4cdf8e2bfadfc3c');
  assert.deepEqual(result.document, { format: 'docx', pageCount: 2, title: '문서관리 규정 (예시)' });
  assert.deepEqual(result.summary, { totalPages: 2, analyzedPages: 2 });
  assert.deepEqual(result.pages[1], { page: 2, category: 'hybrid_text_image', strategy: 'native_text', requiresVision: true });
  assert.equal(result.tables[0]?.caption, '표 1. 문서 종류별 담당 부서');
  assert.deepEqual(result.tables[0]?.headers, ['구분', '담당 부서', '비고']);
  assert.equal(result.tables[0]?.rowCount, 4);
  assert.equal(result.chunks.length, 2);
  assert.equal(result.chunks[1]?.pageEnd, 2);
  assert.equal(result.visuals[0]?.imageDataUrl, TINY_PNG);
  assert.equal(result.visuals[0]?.cropStatus, 'saved');
  assert.deepEqual(result.warnings, []);
});

test('odd or missing fields fall back to empty values', () => {
  const result = normalizeParseResponse({
    bundle_id: 'abc123',
    tables: [{ headers: ['가', 7, null], rows: [[1, true]], caption: '  ' }],
    chunks: 'none',
    visuals: [{ image_data_url: 'javascript:alert(1)' }],
    warnings: ['메모', 3],
  });
  assert.equal(result.markdown, '');
  assert.deepEqual(result.pages, []);
  assert.deepEqual(result.tables[0]?.headers, ['가', '7', '']);
  assert.deepEqual(result.tables[0]?.rows, [['1', 'true']]);
  assert.equal(result.tables[0]?.caption, null);
  assert.equal(result.tables[0]?.rowCount, 2);
  assert.deepEqual(result.chunks, []);
  assert.equal(result.visuals[0]?.imageDataUrl, null);
  assert.deepEqual(result.warnings, ['메모']);
});

test('an answer without bundle_id is a server error', () => {
  assert.throws(
    () => normalizeParseResponse({ markdown: '# 제목' }),
    (error: unknown) => error instanceof AppError && error.kind === 'server',
  );
  assert.throws(() => normalizeParseResponse(null), AppError);
});

test('only base64 raster images are shown', () => {
  assert.equal(safeImageDataUrl(TINY_PNG), TINY_PNG);
  assert.equal(safeImageDataUrl('data:image/jpeg;base64,/9j/4AAQ'), 'data:image/jpeg;base64,/9j/4AAQ');
  assert.equal(safeImageDataUrl('data:image/svg+xml;base64,PHN2Zz4='), null);
  assert.equal(safeImageDataUrl('data:text/html;base64,PGI+'), null);
  assert.equal(safeImageDataUrl('https://example.com/a.png'), null);
  assert.equal(safeImageDataUrl('data:image/png;base64,abc"onerror'), null);
  assert.equal(safeImageDataUrl(42), null);
});

test('the ZIP link points at the local download path', () => {
  assert.equal(downloadPath('6dec1208d4cdf8e2bfadfc3c'), '/api/download/6dec1208d4cdf8e2bfadfc3c');
  assert.equal(downloadPath('a/b?c'), '/api/download/a%2Fb%3Fc');
});

test('parseDocument posts the request and normalizes the answer', async () => {
  let seen: { url: string; body: unknown } | undefined;
  const fetchImpl = (async (url: string, init: RequestInit) => {
    seen = { url, body: JSON.parse(String(init.body)) };
    return new Response(JSON.stringify(sampleRaw()), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }) as typeof fetch;
  const request: ParseRequest = {
    filename: '문서관리_규정_예시.docx',
    content_base64: 'UEsDBA==',
    options: {
      pages: null,
      layout_detector: 'none',
      include_comments: true,
      extract_crops: true,
      include_chunks: true,
      chunk_max_chars: 1500,
      chunk_overlap: 150,
    },
  };
  const result = await parseDocument(request, { fetchImpl });
  assert.equal(seen?.url, '/api/parse');
  assert.deepEqual(seen?.body, request);
  assert.equal(result.tables.length, 1);
});

test('parseDocument turns a 422 into an AppError', async () => {
  const fetchImpl = (async () =>
    new Response(JSON.stringify({ error: '손상된 파일이다' }), { status: 422 })) as unknown as typeof fetch;
  await assert.rejects(
    parseDocument({ filename: 'a.pdf', content_base64: 'JVBERi0=', options: {} as never }, { fetchImpl }),
    (error: unknown) => error instanceof AppError && error.kind === 'unreadable' && error.status === 422,
  );
});
