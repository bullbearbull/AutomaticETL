/**
 * The file formats this tab reads, how each one counts its pages, and the
 * check a file passes in the browser before it is read.
 */
import { AppError, fileTooLargeError, unsupportedFileError } from '../../lib/errors.ts';
import { checkFile, fileExtension, formatBytes, MAX_UPLOAD_BYTES } from '../../lib/files.ts';
import { extractErrorCopy, formatCopy } from './copy.ts';

/** The server's `supported_extensions()` for documents (GET /api/doctor). */
export const SUPPORTED_EXTENSIONS: readonly string[] = [
  '.doc',
  '.docx',
  '.hwp',
  '.hwpx',
  '.pdf',
  '.ppt',
  '.pptx',
  '.xlsx',
];

/** 쪽 for PDF, 한글 and 워드; slides for 파워포인트; sheets for 엑셀. */
export type PageUnit = 'page' | 'slide' | 'sheet';

/** `pdf`, `docx`, … from a response format or a file name. */
export function formatFromName(name: string): string {
  return fileExtension(name).replace(/^\./, '');
}

export function unitForFormat(format: string): PageUnit {
  switch (format.toLowerCase()) {
    case 'ppt':
    case 'pptx':
      return 'slide';
    case 'xlsx':
      return 'sheet';
    default:
      return 'page';
  }
}

/** "워드 문서", "PDF 문서", or "XLS 파일" for a format the tab does not read. */
export function formatName(format: string): string {
  const key = format.toLowerCase();
  return formatCopy.names[key] ?? formatCopy.other(key);
}

/**
 * Checks a dropped or picked file: the extension, the 50MB limit and an
 * empty file. Returns the error to show, or `null` when it can be read.
 */
export function checkExtractFile(file: { name: string; size: number }): AppError | null {
  const check = checkFile(file, { allowedExtensions: SUPPORTED_EXTENSIONS, maxBytes: MAX_UPLOAD_BYTES });
  if (!check.ok && check.reason === 'extension') {
    return unsupportedFileError(check.extension, {
      supported: formatCopy.supportedList,
      detail: extractErrorCopy.detail.extension(file.name, check.extension, SUPPORTED_EXTENSIONS.join(', ')),
    });
  }
  if (!check.ok && check.reason === 'size') {
    const limitText = formatBytes(check.maxBytes);
    const sizeText = formatBytes(check.size);
    const base = fileTooLargeError(sizeText, limitText);
    return new AppError({
      kind: base.kind,
      what: base.what,
      // formatBytes rounds, so a file just over the limit would read "50MB".
      why: sizeText === limitText ? extractErrorCopy.tooLargeJustOver(limitText) : base.why,
      how: base.how,
      detail: extractErrorCopy.detail.size(file.name, check.size, check.maxBytes),
    });
  }
  if (file.size === 0) {
    return new AppError({
      kind: 'unreadable',
      ...extractErrorCopy.empty,
      detail: extractErrorCopy.detail.empty(file.name),
    });
  }
  return null;
}
