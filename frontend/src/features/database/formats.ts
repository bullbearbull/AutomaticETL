import { AppError, fileTooLargeError, unsupportedFileError } from '../../lib/errors.ts';
import { checkFile, fileExtension, formatBytes, MAX_UPLOAD_BYTES } from '../../lib/files.ts';
import { databaseErrorCopy } from './copy.ts';

/** Supported extensions for table upload (matches `src/autoetl/ui/server.py`). */
export const DATABASE_EXTENSIONS = ['.csv', '.tsv', '.xlsx'] as const;

export const SUPPORTED_FORMATS_LABEL = 'CSV(.csv)·TSV(.tsv)·엑셀(.xlsx)';

export const MAX_TOTAL_UPLOAD_BYTES = MAX_UPLOAD_BYTES;

/** Validates a single dropped or picked file before reading/sending. */
export function checkDatabaseFile(file: { name: string; size: number }): AppError | null {
  const checked = checkFile(file, { allowedExtensions: DATABASE_EXTENSIONS, maxBytes: MAX_TOTAL_UPLOAD_BYTES });
  if (!checked.ok) {
    if (checked.reason === 'extension') {
      return unsupportedFileError(checked.extension, { supported: SUPPORTED_FORMATS_LABEL });
    }
    return fileTooLargeError(formatBytes(checked.size), formatBytes(checked.maxBytes));
  }
  if (file.size === 0) {
    return new AppError({
      kind: 'unreadable',
      ...databaseErrorCopy.emptyFile(file.name),
    });
  }
  return null;
}

/** Validates combined upload size against the server's 50MB total limit. */
export function checkCombinedUploadSize(
  files: ReadonlyArray<{ size: number }>,
  maxBytes: number = MAX_TOTAL_UPLOAD_BYTES,
): AppError | null {
  const total = files.reduce((sum, item) => sum + (Number.isFinite(item.size) ? item.size : 0), 0);
  if (total > maxBytes) {
    return fileTooLargeError(formatBytes(total), formatBytes(maxBytes));
  }
  return null;
}

/** Human-readable label for source file kind. */
export function formatKindLabel(sourceKind: string): string {
  switch (sourceKind.toLowerCase()) {
    case 'csv':
      return 'CSV';
    case 'tsv':
      return 'TSV';
    case 'xlsx':
      return '엑셀';
    default:
      return sourceKind ? sourceKind.toUpperCase() : '표 파일';
  }
}

export function extensionOf(name: string): string {
  return fileExtension(name);
}
