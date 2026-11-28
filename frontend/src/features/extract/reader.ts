/**
 * Reads one file: checks the page list, turns the file into base64 and
 * sends it to POST /api/parse. It returns what happened instead of changing
 * any state, so the caller decides what to do with it, and tests can swap
 * the browser parts.
 */
import { isAbortError, toAppError, type AppError } from '../../lib/errors.ts';
import { readFileAsBase64 } from '../../lib/files.ts';
import { parseDocument, type ParseRequest, type ParseResult } from './api.ts';
import { fileReadError, pageSpecError, refineReadError } from './errors.ts';
import { formatFromName, unitForFormat } from './formats.ts';
import { toParseOptions, type ExtractSettings } from './settings.ts';

export type ReadOutcome =
  | { type: 'succeed'; result: ParseResult }
  | { type: 'fail'; error: AppError }
  /** Cancelled because the file left the list; nothing to show. */
  | { type: 'aborted' };

export interface ReadDeps {
  readBase64: (file: Blob, signal: AbortSignal) => Promise<string>;
  parse: (request: ParseRequest, signal: AbortSignal) => Promise<ParseResult>;
}

export const browserReadDeps: ReadDeps = {
  readBase64: (file, signal) => readFileAsBase64(file, { signal }),
  parse: (request, signal) => parseDocument(request, { signal }),
};

export async function readEntry(
  target: { file: Blob; name: string },
  settings: ExtractSettings,
  signal: AbortSignal,
  deps: ReadDeps = browserReadDeps,
): Promise<ReadOutcome> {
  const prepared = toParseOptions(settings);
  if (!prepared.ok) return { type: 'fail', error: pageSpecError(prepared.token) };

  let content: string;
  try {
    content = await deps.readBase64(target.file, signal);
  } catch (error) {
    if (signal.aborted || isAbortError(error)) return { type: 'aborted' };
    return { type: 'fail', error: fileReadError(error) };
  }
  if (signal.aborted) return { type: 'aborted' };

  try {
    const result = await deps.parse(
      { filename: target.name, content_base64: content, options: prepared.options },
      signal,
    );
    return signal.aborted ? { type: 'aborted' } : { type: 'succeed', result };
  } catch (error) {
    const appError = toAppError(error);
    if (signal.aborted || appError.kind === 'aborted') return { type: 'aborted' };
    return { type: 'fail', error: refineReadError(appError, unitForFormat(formatFromName(target.name))) };
  }
}
