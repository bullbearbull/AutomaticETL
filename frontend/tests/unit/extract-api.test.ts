import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  downloadPath,
  fetchZip,
  normalizeParseResponse,
  parseDocument,
  safeImageDataUrl,
  ZIP_FALLBACK_NAME,
  zipFileName,
  type ParseRequest,
  type ZipDeps,
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

/** The header the local server sends: an ASCII name and the UTF-8 name. */
function disposition(utf8Name: string, asciiName = 'autoetl_bundle.zip'): string {
  return `attachment; filename="${asciiName}"; filename*=UTF-8''${encodeURIComponent(utf8Name)}`;
}

test('the saved ZIP takes the UTF-8 name first', () => {
  assert.equal(zipFileName(disposition('문서관리_규정_예시_autoetl_bundle.zip')), '문서관리_규정_예시_autoetl_bundle.zip');
  assert.equal(zipFileName(`ATTACHMENT; FILENAME*=utf-8'ko'${encodeURIComponent('표 모음.zip')}`), '표 모음.zip');
});

test('without a usable UTF-8 name the plain name is used', () => {
  assert.equal(zipFileName('attachment; filename="report.zip"'), 'report.zip');
  assert.equal(zipFileName('attachment; filename=plain.zip'), 'plain.zip');
  assert.equal(zipFileName('attachment; filename="a \\"b\\".zip"'), 'a "b".zip');
  // A broken %-escape and a charset other than UTF-8 are skipped.
  assert.equal(zipFileName(`attachment; filename="safe.zip"; filename*=UTF-8''%E0%A4%A.zip`), 'safe.zip');
  assert.equal(zipFileName(`attachment; filename="latin.zip"; filename*=ISO-8859-1''%E9t%E9.zip`), 'latin.zip');
});

test('without any name the ZIP gets the fallback name', () => {
  assert.equal(ZIP_FALLBACK_NAME, 'autoetl_bundle.zip');
  assert.equal(zipFileName(null), ZIP_FALLBACK_NAME);
  assert.equal(zipFileName('attachment'), ZIP_FALLBACK_NAME);
  assert.equal(zipFileName(`attachment; filename*=UTF-8''%ZZ`), ZIP_FALLBACK_NAME);
  assert.equal(zipFileName('attachment; filename=""'), ZIP_FALLBACK_NAME);
});

test('a ZIP name never points into a folder', () => {
  assert.equal(zipFileName(`attachment; filename*=UTF-8''${encodeURIComponent('../../결과.zip')}`), '결과.zip');
  assert.equal(zipFileName('attachment; filename="C:\\\\temp\\\\x.zip"'), 'x.zip');
});

const ZIP_BYTES = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00]);

function zipDeps(answer: () => Promise<Response>, seen: { url?: string; init?: RequestInit } = {}): ZipDeps {
  return {
    fetch: async (url, init) => {
      seen.url = url;
      seen.init = init;
      return answer();
    },
  };
}

test('fetchZip asks for the ZIP and hands back its bytes and name', async () => {
  const seen: { url?: string; init?: RequestInit } = {};
  const controller = new AbortController();
  const deps = zipDeps(
    async () =>
      new Response(ZIP_BYTES, {
        status: 200,
        headers: { 'Content-Type': 'application/zip', 'Content-Disposition': disposition('예시_autoetl_bundle.zip') },
      }),
    seen,
  );
  const outcome = await fetchZip('6dec1208d4cdf8e2bfadfc3c', controller.signal, deps);
  assert.equal(seen.url, '/api/download/6dec1208d4cdf8e2bfadfc3c');
  assert.equal(seen.init?.credentials, 'same-origin');
  assert.equal(seen.init?.signal, controller.signal);
  assert.equal(outcome.type, 'ok');
  if (outcome.type !== 'ok') return;
  assert.equal(outcome.fileName, '예시_autoetl_bundle.zip');
  assert.deepEqual(new Uint8Array(await outcome.blob.arrayBuffer()), ZIP_BYTES);
});

test('fetchZip tells a ZIP the server forgot (404) from other failures', async () => {
  const signal = new AbortController().signal;
  const notFound = zipDeps(async () => new Response(JSON.stringify({ error: '요청한 번들을 찾을 수 없다.' }), { status: 404 }));
  assert.deepEqual(await fetchZip('abc', signal, notFound), { type: 'missing' });
  const broken = zipDeps(async () => new Response('', { status: 500 }));
  assert.deepEqual(await fetchZip('abc', signal, broken), { type: 'failed', status: 500 });
  const offline = zipDeps(async () => {
    throw new TypeError('Failed to fetch');
  });
  assert.deepEqual(await fetchZip('abc', signal, offline), { type: 'failed', status: null });
});

test('fetchZip reports a cancelled request as aborted', async () => {
  const controller = new AbortController();
  controller.abort();
  const cancelled = zipDeps(async () => {
    throw new DOMException('The operation was aborted.', 'AbortError');
  });
  assert.deepEqual(await fetchZip('abc', controller.signal, cancelled), { type: 'aborted' });
  // Cancelled while the bytes were coming in.
  const late = new AbortController();
  const slow = zipDeps(async () => {
    late.abort();
    return new Response(ZIP_BYTES, { status: 200 });
  });
  assert.deepEqual(await fetchZip('abc', late.signal, slow), { type: 'aborted' });
});
