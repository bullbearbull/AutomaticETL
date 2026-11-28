import { AppError, fileTooLargeError, unsupportedFileError } from '../../lib/errors.ts';
import { checkFile, fileExtension, formatBytes, MAX_UPLOAD_BYTES } from '../../lib/files.ts';
import { searchErrorCopy } from './copy.ts';

/** Supported document formats for `/api/docindex/build` (`supported_extensions()`). */
export const SEARCH_EXTENSIONS = [
  '.doc',
  '.docx',
  '.hwp',
  '.hwpx',
  '.pdf',
  '.ppt',
  '.pptx',
  '.xlsx',
] as const;

export const SUPPORTED_DOCUMENTS_LABEL =
  'PDF · 한글(HWP·HWPX) · 워드(DOC·DOCX) · 파워포인트(PPT·PPTX) · 엑셀(XLSX)';

export type SearchPageUnit = '쪽' | '슬라이드' | '시트';

/** Validates a single dropped or picked document before reading/sending. */
export function checkSearchFile(file: { name: string; size: number }): AppError | null {
  const checked = checkFile(file, {
    allowedExtensions: SEARCH_EXTENSIONS,
    maxBytes: MAX_UPLOAD_BYTES,
  });
  if (!checked.ok) {
    if (checked.reason === 'extension') {
      return unsupportedFileError(checked.extension, { supported: SUPPORTED_DOCUMENTS_LABEL });
    }
    return fileTooLargeError(formatBytes(checked.size), formatBytes(checked.maxBytes));
  }
  if (file.size === 0) {
    return new AppError({
      kind: 'unreadable',
      ...searchErrorCopy.emptyFile(file.name),
    });
  }
  return null;
}

/** Validates combined upload size against the server's 50MB total limit. */
export function checkCombinedUploadSize(
  files: ReadonlyArray<{ size: number }>,
  maxBytes: number = MAX_UPLOAD_BYTES,
): AppError | null {
  const total = files.reduce((sum, item) => sum + (Number.isFinite(item.size) ? item.size : 0), 0);
  if (total > maxBytes) {
    return fileTooLargeError(formatBytes(total), formatBytes(maxBytes));
  }
  return null;
}

/** Extracts the display file name from a full or relative path. */
export function baseFileName(sourcePath: string): string {
  const name = sourcePath.split(/[\\/]/).pop() ?? '';
  return name || sourcePath;
}

/** Returns the human-readable page unit matching the document format or file extension. */
export function pageUnitFor(formatOrName: string): SearchPageUnit {
  const cleaned = formatOrName.startsWith('.')
    ? formatOrName.slice(1).toLowerCase()
    : formatOrName.includes('.')
      ? fileExtension(formatOrName).slice(1).toLowerCase()
      : formatOrName.toLowerCase();
  if (cleaned === 'ppt' || cleaned === 'pptx') return '슬라이드';
  if (cleaned === 'xlsx') return '시트';
  return '쪽';
}

/** Formats a single page (`3쪽`) or span (`3–5쪽`). */
export function formatPageRange(start: number, end: number, unit: SearchPageUnit = '쪽'): string {
  const safeStart = Math.max(1, Math.trunc(start));
  const safeEnd = Math.max(safeStart, Math.trunc(end));
  return safeEnd > safeStart ? `${safeStart}–${safeEnd}${unit}` : `${safeStart}${unit}`;
}
