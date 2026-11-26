/**
 * AppError carries the 무슨 일 · 왜 · 어떻게 message the screen shows, plus
 * the raw server or browser text for the 자세히 disclosure. Server messages
 * are never changed on the server; this module maps them on the client by
 * HTTP status and known message prefixes.
 */
import { errorCopy } from '../copy/common.ts';

export type AppErrorKind =
  | 'network'
  | 'forbidden'
  | 'tooLarge'
  | 'unsupportedFile'
  | 'expired'
  | 'unreadable'
  | 'request'
  | 'server'
  | 'aborted';

export interface AppErrorInit {
  kind: AppErrorKind;
  /** 무슨 일: what happened, one sentence (the notice title). */
  what: string;
  /** 왜: a reason the user can understand. */
  why: string;
  /** 어떻게: what the user can do next. */
  how: string;
  /** Raw text from the server or browser, shown folded under 자세히. */
  detail?: string | undefined;
  /** HTTP status when the error came from a response. */
  status?: number | undefined;
}

export class AppError extends Error {
  readonly kind: AppErrorKind;
  readonly what: string;
  readonly why: string;
  readonly how: string;
  readonly detail: string | undefined;
  readonly status: number | undefined;

  constructor(init: AppErrorInit) {
    super(init.what);
    this.name = 'AppError';
    this.kind = init.kind;
    this.what = init.what;
    this.why = init.why;
    this.how = init.how;
    this.detail = init.detail;
    this.status = init.status;
  }
}

export function isAppError(value: unknown): value is AppError {
  return value instanceof AppError;
}

/** Server message prefixes (Korean, 해라체) that this module recognises. */
const UNSUPPORTED_PREFIXES = [
  '지원하지 않는 파일 형식이다',
  '지원하지 않는 문서 파일 형식이다',
  '지원하지 않는 테이블 파일 형식이다',
  '지원하지 않는 문서 형식이다',
];
const TOO_LARGE_PREFIXES = ['업로드 파일 크기가', '업로드 파일 합계 크기가', '요청 본문 크기가'];
const EXPIRED_PREFIXES = ['요청한 번들을 찾을 수 없다', '문서 인덱스를 찾을 수 없다', '분석 세션을 찾을 수 없'];

function startsWithAny(text: string, prefixes: readonly string[]): boolean {
  return prefixes.some((prefix) => text.startsWith(prefix));
}

/** Pulls `.xls` out of a server message such as `지원하지 않는 파일 형식이다: '.xls' (지원: …)`. */
export function extensionFromServerMessage(message: string): string {
  const match = /'(\.[^']*)'/.exec(message);
  return match?.[1]?.toLowerCase() ?? '';
}

/**
 * The error for a file the screen cannot read. `.xls` gets the
 * "save as .xlsx" instructions; other extensions get a generic hint.
 * `supported` is a readable list such as "PDF·한글·워드·파워포인트·엑셀(XLSX)".
 */
export function unsupportedFileError(
  extension: string,
  options: { supported?: string; detail?: string; status?: number } = {},
): AppError {
  const copy = errorCopy.unsupportedFile;
  const ext = extension.toLowerCase();
  const isXls = ext === '.xls';
  let why: string;
  if (isXls) why = copy.xlsWhy;
  else if (options.supported) why = copy.whyWithList(ext, options.supported);
  else why = copy.why(ext);
  return new AppError({
    kind: 'unsupportedFile',
    what: copy.what,
    why,
    how: isXls ? copy.xlsHow : copy.how,
    detail: options.detail,
    status: options.status,
  });
}

/** The error for a file over the size limit, checked in the browser before sending. */
export function fileTooLargeError(sizeText: string, limitText: string): AppError {
  const copy = errorCopy.fileTooLarge;
  return new AppError({ kind: 'tooLarge', what: copy.what, why: copy.why(sizeText, limitText), how: copy.how });
}

/** A fetch that never reached the server (fetch throws a TypeError). */
export function networkError(cause?: unknown): AppError {
  return new AppError({ kind: 'network', ...errorCopy.network, detail: describeCause(cause) });
}

/** A request cancelled through its AbortSignal. Screens usually ignore it. */
export function abortedError(cause?: unknown): AppError {
  return new AppError({ kind: 'aborted', ...errorCopy.aborted, detail: describeCause(cause) });
}

/**
 * Maps an HTTP error response to an AppError. `serverMessage` is the
 * `error` field of the server's JSON body (or the raw body text).
 */
export function errorFromResponse(status: number, serverMessage: string): AppError {
  const message = serverMessage.trim();
  const base = { detail: message ? `HTTP ${status} · ${message}` : `HTTP ${status}`, status };

  if (startsWithAny(message, UNSUPPORTED_PREFIXES)) {
    return unsupportedFileError(extensionFromServerMessage(message), base);
  }
  if (status === 413 || startsWithAny(message, TOO_LARGE_PREFIXES)) {
    return new AppError({ kind: 'tooLarge', ...errorCopy.tooLarge, ...base });
  }
  if (status === 403) {
    return new AppError({ kind: 'forbidden', ...errorCopy.forbidden, ...base });
  }
  if (status === 404 && startsWithAny(message, EXPIRED_PREFIXES)) {
    return new AppError({ kind: 'expired', ...errorCopy.expired, ...base });
  }
  if (status === 422) {
    return new AppError({ kind: 'unreadable', ...errorCopy.unreadable, ...base });
  }
  if (status >= 500) {
    return new AppError({ kind: 'server', ...errorCopy.server, ...base });
  }
  return new AppError({ kind: 'request', ...errorCopy.request, ...base });
}

/** Turns anything thrown into an AppError, keeping AppErrors as they are. */
export function toAppError(value: unknown): AppError {
  if (value instanceof AppError) return value;
  if (isAbortError(value)) return abortedError(value);
  if (value instanceof TypeError) return networkError(value);
  return new AppError({ kind: 'server', ...errorCopy.server, detail: describeCause(value) });
}

export function isAbortError(value: unknown): boolean {
  return (
    typeof value === 'object' &&
    value !== null &&
    'name' in value &&
    (value as { name: unknown }).name === 'AbortError'
  );
}

function describeCause(cause: unknown): string | undefined {
  if (cause === undefined || cause === null) return undefined;
  if (cause instanceof Error) return cause.message ? `${cause.name}: ${cause.message}` : cause.name;
  return String(cause);
}
