/**
 * Requests and responses of `POST /api/parse` and of the ZIP at
 * `GET /api/download/<bundle_id>`. The parse response is checked field by
 * field, so a missing or odd value becomes an empty one instead of breaking
 * the screen.
 */
import { postJson, type RequestOptions } from '../../api/http.ts';
import { errorCopy } from '../../copy/common.ts';
import { AppError, isAbortError } from '../../lib/errors.ts';
import { extractErrorCopy } from './copy.ts';

/** `options` of POST /api/parse. */
export interface ParseOptions {
  /** Canonical page selection such as "1-3,5"; `null` reads every page. */
  pages: string | null;
  layout_detector: 'none' | 'heuristic';
  include_comments: boolean;
  extract_crops: boolean;
  include_chunks: boolean;
  /** 200–20000. */
  chunk_max_chars: number;
  /** 0–2000. */
  chunk_overlap: number;
}

export interface ParseRequest {
  filename: string;
  content_base64: string;
  options: ParseOptions;
}

export interface DocumentInfo {
  /** `pdf`, `docx`, `hwp`, … */
  format: string;
  pageCount: number;
  title: string | null;
}

export interface RoutingPage {
  page: number;
  category: string;
  strategy: string;
  requiresVision: boolean;
}

export interface RoutingSummary {
  totalPages: number;
  analyzedPages: number;
}

export interface ExtractedTable {
  id: string;
  page: number;
  caption: string | null;
  strategy: string;
  rowCount: number;
  colCount: number;
  headers: string[];
  rows: string[][];
  /** The table as GFM markdown, exactly as it appears in `markdown`. */
  markdown: string;
}

export interface TextChunk {
  id: string;
  pageStart: number;
  pageEnd: number;
  charCount: number;
  text: string;
}

export interface VisualRegion {
  id: string;
  page: number;
  kind: string;
  reason: string;
  caption: string | null;
  cropStatus: string;
  /** A `data:image/...;base64,` URL, or `null` when no image was kept. */
  imageDataUrl: string | null;
}

export interface ParseResult {
  bundleId: string;
  document: DocumentInfo;
  pages: RoutingPage[];
  summary: RoutingSummary;
  markdown: string;
  routingReport: string;
  tables: ExtractedTable[];
  chunks: TextChunk[];
  visuals: VisualRegion[];
  warnings: string[];
}

/** Reads one file. Rejects with an AppError. */
export async function parseDocument(request: ParseRequest, options: RequestOptions = {}): Promise<ParseResult> {
  const raw = await postJson<unknown>('/api/parse', request, options);
  return normalizeParseResponse(raw);
}

/** Where the ZIP of a result is fetched from. */
export function downloadPath(bundleId: string): string {
  return `/api/download/${encodeURIComponent(bundleId)}`;
}

/** The name a saved ZIP gets when the server sends none. */
export const ZIP_FALLBACK_NAME = 'autoetl_bundle.zip';

/** The last part of a path, so a name can never point into a folder. */
function baseName(name: string): string {
  return (name.split(/[\\/]/).pop() ?? '').trim();
}

/**
 * The file name in a Content-Disposition header: `filename*=UTF-8''…`
 * first, then `filename="…"`, then ZIP_FALLBACK_NAME.
 */
export function zipFileName(contentDisposition: string | null): string {
  const header = contentDisposition ?? '';
  const extended = /(?:^|;)\s*filename\*\s*=\s*([^';]*)'[^';]*'([^;]*)/i.exec(header);
  if (extended && (extended[1] ?? '').trim().toLowerCase() === 'utf-8') {
    try {
      const name = baseName(decodeURIComponent((extended[2] ?? '').trim()));
      if (name) return name;
    } catch {
      // A broken %-escape: use the plain name below.
    }
  }
  const plain = /(?:^|;)\s*filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;\s]+))/i.exec(header);
  if (plain) {
    const name = baseName((plain[1] ?? plain[2] ?? '').replace(/\\(.)/g, '$1'));
    if (name) return name;
  }
  return ZIP_FALLBACK_NAME;
}

/** What GET /api/download/<bundle_id> gave back. */
export type ZipOutcome =
  | { type: 'ok'; blob: Blob; fileName: string }
  /** 404: the server no longer keeps this ZIP. */
  | { type: 'missing' }
  /** No answer, or another status. `status` is `null` when no answer came. */
  | { type: 'failed'; status: number | null }
  /** Cancelled because the export bar went away. */
  | { type: 'aborted' };

/** The browser part of fetchZip, swapped out in tests. */
export interface ZipDeps {
  fetch: (url: string, init: RequestInit) => Promise<Response>;
}

export const browserZipDeps: ZipDeps = {
  fetch: (url, init) => fetch(url, init),
};

/**
 * Fetches the ZIP of a result and sorts out the answer. The server keeps
 * only the last 10 ZIPs it made, for the classic page and other tools too,
 * and forgets them all when it restarts, so asking is the only sure way to
 * know this ZIP is still there.
 */
export async function fetchZip(bundleId: string, signal: AbortSignal, deps: ZipDeps = browserZipDeps): Promise<ZipOutcome> {
  let response: Response;
  try {
    response = await deps.fetch(downloadPath(bundleId), { credentials: 'same-origin', signal });
  } catch (error) {
    return signal.aborted || isAbortError(error) ? { type: 'aborted' } : { type: 'failed', status: null };
  }
  if (response.status === 404) return { type: 'missing' };
  if (response.status !== 200) return { type: 'failed', status: response.status };
  try {
    const blob = await response.blob();
    if (signal.aborted) return { type: 'aborted' };
    return { type: 'ok', blob, fileName: zipFileName(response.headers.get('Content-Disposition')) };
  } catch (error) {
    return signal.aborted || isAbortError(error) ? { type: 'aborted' } : { type: 'failed', status: response.status };
  }
}

/** Image data URLs the screen shows; anything else is dropped. */
const IMAGE_DATA_URL = /^data:image\/(?:png|jpeg|gif|webp|bmp);base64,[A-Za-z0-9+/]+=*$/;

export function safeImageDataUrl(value: unknown): string | null {
  return typeof value === 'string' && IMAGE_DATA_URL.test(value) ? value : null;
}

type Json = Record<string, unknown>;

function record(value: unknown): Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? (value as Json) : {};
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function optionalText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

function count(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : fallback;
}

function cell(value: unknown): string {
  if (value === null || value === undefined) return '';
  return typeof value === 'string' ? value : String(value);
}

/**
 * Turns the raw JSON of POST /api/parse into a ParseResult. Only
 * `bundle_id` is required (the ZIP link needs it); everything else falls
 * back to an empty value.
 */
export function normalizeParseResponse(raw: unknown): ParseResult {
  const body = record(raw);
  const bundleId = text(body.bundle_id).trim();
  if (!bundleId) {
    throw new AppError({
      kind: 'server',
      ...errorCopy.server,
      detail: extractErrorCopy.detail.noBundleId,
    });
  }

  const document = record(body.document);
  const plan = record(body.routing_plan);
  const summary = record(plan.summary);
  const pageCount = count(document.page_count);

  return {
    bundleId,
    document: {
      format: text(document.format).toLowerCase(),
      pageCount,
      title: optionalText(document.title),
    },
    pages: list(plan.pages).map((item) => {
      const page = record(item);
      return {
        page: count(page.page),
        category: text(page.category),
        strategy: text(page.primary_strategy),
        requiresVision: page.requires_vision === true,
      };
    }),
    summary: {
      totalPages: count(summary.total_pages, pageCount),
      analyzedPages: count(summary.analyzed_pages, pageCount),
    },
    markdown: text(body.markdown),
    routingReport: text(body.routing_report_markdown),
    tables: list(body.tables).map((item, index) => {
      const table = record(item);
      const headers = list(table.headers).map(cell);
      const rows = list(table.rows).map((row) => list(row).map(cell));
      return {
        id: text(table.table_id, `table-${index + 1}`),
        page: count(table.page),
        caption: optionalText(table.caption),
        strategy: text(table.strategy),
        rowCount: count(table.row_count, rows.length + (headers.length > 0 ? 1 : 0)),
        colCount: count(table.col_count, headers.length),
        headers,
        rows,
        markdown: text(table.markdown),
      };
    }),
    chunks: list(body.chunks).map((item, index) => {
      const chunk = record(item);
      const chunkText = text(chunk.text);
      return {
        id: text(chunk.chunk_id, `chunk-${index + 1}`),
        pageStart: count(chunk.page_start),
        pageEnd: count(chunk.page_end),
        charCount: count(chunk.char_count, chunkText.length),
        text: chunkText,
      };
    }),
    visuals: list(body.visuals).map((item, index) => {
      const visual = record(item);
      return {
        id: text(visual.region_id, `visual-${index + 1}`),
        page: count(visual.page),
        kind: text(visual.element_kind),
        reason: text(visual.reason),
        caption: optionalText(visual.caption),
        cropStatus: text(visual.crop_status),
        imageDataUrl: safeImageDataUrl(visual.image_data_url),
      };
    }),
    warnings: list(body.warnings).filter((warning): warning is string => typeof warning === 'string'),
  };
}
