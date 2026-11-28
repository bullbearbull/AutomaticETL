/**
 * Generic file helpers. Per-tab extension lists live in each tab folder;
 * pass them to `checkFile`.
 */

/** The server's upload limit for one file (50MB). */
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

/** Lower-case extension with the dot (`'.pdf'`), or `''` when there is none. */
export function fileExtension(name: string): string {
  const base = name.split(/[\\/]/).pop() ?? '';
  const dot = base.lastIndexOf('.');
  if (dot <= 0 || dot === base.length - 1) return '';
  return base.slice(dot).toLowerCase();
}

/**
 * A size with a unit, as the copy guide asks (`12KB`, `3.4MB`, `50MB`).
 * Uses 1024-based units, matching the server's 50MB limit.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${bytes}B`;
  const units = ['KB', 'MB', 'GB'] as const;
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = value >= 10 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded}${units[unit]}`;
}

export type FileCheck =
  | { ok: true; extension: string }
  | { ok: false; reason: 'extension'; extension: string }
  | { ok: false; reason: 'size'; extension: string; size: number; maxBytes: number };

/**
 * Checks a picked file before it is read or sent. `allowedExtensions` holds
 * lower-case extensions with the dot, for example `['.pdf', '.docx']`.
 */
export function checkFile(
  file: { name: string; size: number },
  options: { allowedExtensions: readonly string[]; maxBytes?: number },
): FileCheck {
  const extension = fileExtension(file.name);
  if (!options.allowedExtensions.includes(extension)) {
    return { ok: false, reason: 'extension', extension };
  }
  const maxBytes = options.maxBytes ?? MAX_UPLOAD_BYTES;
  if (file.size > maxBytes) {
    return { ok: false, reason: 'size', extension, size: file.size, maxBytes };
  }
  return { ok: true, extension };
}

/** The base64 part of a `data:` URL (`data:application/pdf;base64,AAAA` → `AAAA`). */
export function base64FromDataUrl(dataUrl: string): string {
  const comma = dataUrl.indexOf(',');
  if (!dataUrl.startsWith('data:') || comma < 0 || !dataUrl.slice(0, comma).endsWith(';base64')) {
    throw new Error('base64 data URL이 아니에요.');
  }
  return dataUrl.slice(comma + 1);
}

interface DataUrlReader {
  result: string | ArrayBuffer | null;
  error: unknown;
  onload: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onabort: ((event: unknown) => void) | null;
  readAsDataURL(blob: Blob): void;
  abort(): void;
}

/**
 * Reads a file as base64 through `FileReader.readAsDataURL`, which handles
 * large files without building a huge string by hand. `signal` aborts the read.
 */
export function readFileAsBase64(
  file: Blob,
  options: { signal?: AbortSignal; createReader?: () => DataUrlReader } = {},
): Promise<string> {
  const createReader = options.createReader ?? (() => new FileReader() as unknown as DataUrlReader);
  return new Promise<string>((resolve, reject) => {
    const { signal } = options;
    if (signal?.aborted) {
      reject(signal.reason ?? new DOMException('Aborted', 'AbortError'));
      return;
    }
    const reader = createReader();
    const onAbortSignal = () => reader.abort();
    const cleanup = () => signal?.removeEventListener('abort', onAbortSignal);
    reader.onload = () => {
      cleanup();
      try {
        resolve(base64FromDataUrl(String(reader.result ?? '')));
      } catch (error) {
        reject(error);
      }
    };
    reader.onerror = () => {
      cleanup();
      reject(reader.error ?? new Error('파일을 읽지 못했어요.'));
    };
    reader.onabort = () => {
      cleanup();
      reject(signal?.reason ?? new DOMException('Aborted', 'AbortError'));
    };
    signal?.addEventListener('abort', onAbortSignal, { once: true });
    reader.readAsDataURL(file);
  });
}
