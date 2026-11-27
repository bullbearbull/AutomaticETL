/**
 * Requests and responses of `POST /api/parse` and the ZIP link
 * `GET /api/download/<bundle_id>`. The response is checked field by field,
 * so a missing or odd value becomes an empty one instead of breaking the
 * screen.
 */
import { postJson, type RequestOptions } from '../../api/http.ts';
import { errorCopy } from '../../copy/common.ts';
import { AppError } from '../../lib/errors.ts';
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

/** The ZIP link of a result. */
export function downloadPath(bundleId: string): string {
  return `/api/download/${encodeURIComponent(bundleId)}`;
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
