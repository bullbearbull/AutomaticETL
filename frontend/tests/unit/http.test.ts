import assert from 'node:assert/strict';
import { test } from 'node:test';
import { getJson, postJson, serverMessageFrom } from '../../src/api/http.ts';
import { AppError } from '../../src/lib/errors.ts';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
}

test('postJson sends JSON with the right headers and parses the reply', async () => {
  let seen: { url: string; init: RequestInit } | undefined;
  const fetchImpl = (async (url: string, init: RequestInit) => {
    seen = { url, init };
    return jsonResponse(200, { ok: true, count: 2 });
  }) as typeof fetch;
  const result = await postJson<{ ok: boolean; count: number }>('/api/parse', { filename: '예시.pdf' }, { fetchImpl });
  assert.deepEqual(result, { ok: true, count: 2 });
  assert.equal(seen?.url, '/api/parse');
  assert.equal(seen?.init.method, 'POST');
  assert.equal((seen?.init.headers as Record<string, string>)['Content-Type'], 'application/json');
  assert.equal(seen?.init.body, JSON.stringify({ filename: '예시.pdf' }));
});

test('getJson sends no body', async () => {
  let init: RequestInit | undefined;
  const fetchImpl = (async (_url: string, requestInit: RequestInit) => {
    init = requestInit;
    return jsonResponse(200, { python: '3.12' });
  }) as typeof fetch;
  assert.deepEqual(await getJson('/api/doctor', { fetchImpl }), { python: '3.12' });
  assert.equal(init?.method, 'GET');
  assert.equal(init?.body, undefined);
});

test('error responses become AppErrors with the server text under detail', async () => {
  const fetchImpl = (async () =>
    jsonResponse(400, { error: "지원하지 않는 파일 형식이다: '.xls' (지원: .pdf)" })) as unknown as typeof fetch;
  await assert.rejects(postJson('/api/parse', {}, { fetchImpl }), (error: unknown) => {
    assert.ok(error instanceof AppError);
    assert.equal(error.kind, 'unsupportedFile');
    assert.equal(error.status, 400);
    assert.ok(error.detail?.includes("'.xls'"));
    return true;
  });
});

test('a fetch TypeError becomes a network error', async () => {
  const fetchImpl = (async () => {
    throw new TypeError('Failed to fetch');
  }) as unknown as typeof fetch;
  await assert.rejects(getJson('/api/doctor', { fetchImpl }), (error: unknown) => {
    assert.ok(error instanceof AppError);
    assert.equal(error.kind, 'network');
    return true;
  });
});

test('the AbortSignal is passed on and an abort becomes kind aborted', async () => {
  const controller = new AbortController();
  const fetchImpl = ((_url: string, init: RequestInit) =>
    new Promise((_resolve, reject) => {
      assert.equal(init.signal, controller.signal);
      init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    })) as typeof fetch;
  const promise = postJson('/api/parse', {}, { signal: controller.signal, fetchImpl });
  controller.abort();
  await assert.rejects(promise, (error: unknown) => error instanceof AppError && error.kind === 'aborted');
});

test('a non-JSON success body becomes a server error', async () => {
  const fetchImpl = (async () => new Response('<html>', { status: 200 })) as unknown as typeof fetch;
  await assert.rejects(getJson('/api/doctor', { fetchImpl }), (error: unknown) => {
    assert.ok(error instanceof AppError);
    assert.equal(error.kind, 'server');
    return true;
  });
});

test('serverMessageFrom reads the error field or falls back to text', () => {
  assert.equal(serverMessageFrom('{"error":"알 수 없는 경로다."}'), '알 수 없는 경로다.');
  assert.equal(serverMessageFrom('{"detail":1}'), '{"detail":1}');
  assert.equal(serverMessageFrom('Bad Gateway'), 'Bad Gateway');
});
