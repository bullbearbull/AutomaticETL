/**
 * Errors this tab adds to the shared ones in lib/errors.ts, and which next
 * step each error offers.
 */
import { AppError, type AppErrorKind } from '../../lib/errors.ts';
import { extractErrorCopy } from './copy.ts';
import type { PageUnit } from './formats.ts';

/** The page list in 세부 설정 has a part that is not a page number. */
export function pageSpecError(token: string): AppError {
  const copy = extractErrorCopy.pageSpec;
  return new AppError({ kind: 'request', what: copy.what, why: copy.why(token), how: copy.how });
}

/** The browser could not read the picked file (moved, deleted or locked). */
export function fileReadError(cause: unknown): AppError {
  const detail =
    cause instanceof Error ? (cause.message ? `${cause.name}: ${cause.message}` : cause.name) : String(cause ?? '');
  return new AppError({ kind: 'unreadable', ...extractErrorCopy.fileRead, detail: detail || undefined });
}

const PAGE_RANGE_PATTERNS = [
  /범위\(1~(\d+)\)를 벗어났다/,
  /범위를 벗어났다\(총 (\d+)페이지\)/,
];

/** The last page in a "page out of range" server message, or `null`. */
export function lastPageFromMessage(message: string): number | null {
  for (const pattern of PAGE_RANGE_PATTERNS) {
    const match = pattern.exec(message);
    if (match) return Number(match[1]);
  }
  return null;
}

/**
 * Makes a shared error more specific where this tab knows more: a page
 * list past the end of the file, and a file locked with a password. Both
 * come back from the server as 422 and would otherwise read "이 파일을
 * 읽지 못했어요".
 */
export function refineReadError(error: AppError, unit: PageUnit): AppError {
  if (error.status !== 422 || error.detail === undefined) return error;
  const lastPage = lastPageFromMessage(error.detail);
  if (lastPage !== null) {
    const copy = extractErrorCopy.pageRange;
    return new AppError({
      kind: 'request',
      what: copy.what,
      why: copy.why[unit](lastPage),
      how: copy.how,
      detail: error.detail,
      status: error.status,
    });
  }
  if (/암호/.test(error.detail)) {
    return new AppError({ kind: 'unreadable', ...extractErrorCopy.encrypted, detail: error.detail, status: error.status });
  }
  return error;
}

/**
 * The next step an error offers. A file the tab cannot read gets [다른 파일
 * 넣기]; a problem on the way (connection, server, settings) gets [다시 읽기].
 */
export function errorAction(kind: AppErrorKind): 'pickOther' | 'retry' {
  switch (kind) {
    case 'unsupportedFile':
    case 'tooLarge':
    case 'unreadable':
      return 'pickOther';
    default:
      return 'retry';
  }
}
