/**
 * JSON helpers for the local AutoETL server (`/api/*`). Every failure becomes
 * an AppError (see lib/errors.ts), so screens only deal with one error type.
 * Request and response shapes belong in each tab's own `api.ts`.
 */
import { AppError, errorFromResponse, isAbortError, networkError, abortedError } from '../lib/errors.ts';
import { errorCopy } from '../copy/common.ts';

export interface RequestOptions {
  /** Cancels the request; the promise then rejects with kind `aborted`. */
  signal?: AbortSignal | undefined;
  /** Replaces the global fetch (tests). */
  fetchImpl?: typeof fetch | undefined;
}

const JSON_HEADERS = { 'Content-Type': 'application/json', Accept: 'application/json' } as const;

/** GET `path` and parse the JSON body. */
export function getJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return requestJson<T>(path, { method: 'GET', headers: { Accept: 'application/json' } }, options);
}

/** POST `body` as JSON to `path` and parse the JSON response. */
export function postJson<T>(path: string, body: unknown, options: RequestOptions = {}): Promise<T> {
  return requestJson<T>(path, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }, options);
}

async function requestJson<T>(path: string, init: RequestInit, options: RequestOptions): Promise<T> {
  const fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  let response: Response;
  try {
    response = await fetchImpl(path, { ...init, signal: options.signal ?? null, credentials: 'same-origin' });
  } catch (error) {
    if (isAbortError(error) || options.signal?.aborted) throw abortedError(error);
    throw networkError(error);
  }

  let text: string;
  try {
    text = await response.text();
  } catch (error) {
    if (isAbortError(error) || options.signal?.aborted) throw abortedError(error);
    throw networkError(error);
  }

  if (!response.ok) {
    throw errorFromResponse(response.status, serverMessageFrom(text));
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new AppError({
      kind: 'server',
      ...errorCopy.server,
      detail: `HTTP ${response.status} · ${text.slice(0, 500)}`,
      status: response.status,
    });
  }
}

/** The server answers errors as `{"error": "..."}`; fall back to the raw text. */
export function serverMessageFrom(text: string): string {
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed === 'object' && parsed !== null && 'error' in parsed) {
      const value = (parsed as { error: unknown }).error;
      if (typeof value === 'string') return value;
    }
  } catch {
    // Not JSON: use the text as it is.
  }
  return text.slice(0, 500);
}
