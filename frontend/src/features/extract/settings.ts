/**
 * The seven reading options (세부 설정) as the form holds them, and how they
 * become the `options` of POST /api/parse. Text fields keep what the user
 * typed; the numbers and page list are cleaned up only when sent.
 */
import type { ParseOptions } from './api.ts';

export interface ExtractSettings {
  /** As typed, for example "1-3, 5". Empty reads every page. */
  pages: string;
  /** Find text, table and figure areas on scanned pages (layout_detector "heuristic"). */
  findLayout: boolean;
  includeComments: boolean;
  extractCrops: boolean;
  includeChunks: boolean;
  /** As typed; see CHUNK_MAX_CHARS. */
  chunkMaxChars: string;
  /** As typed; see CHUNK_OVERLAP. */
  chunkOverlap: string;
}

export interface CountRange {
  min: number;
  max: number;
  fallback: number;
}

/** The server clamps to the same ranges. */
export const CHUNK_MAX_CHARS: CountRange = { min: 200, max: 20000, fallback: 1500 };
export const CHUNK_OVERLAP: CountRange = { min: 0, max: 2000, fallback: 150 };

/** The highest page number the field accepts. */
export const MAX_PAGE_NUMBER = 99999;

export const DEFAULT_SETTINGS: ExtractSettings = {
  pages: '',
  findLayout: false,
  includeComments: true,
  extractCrops: true,
  includeChunks: true,
  chunkMaxChars: String(CHUNK_MAX_CHARS.fallback),
  chunkOverlap: String(CHUNK_OVERLAP.fallback),
};

export type PageSpecResult = { ok: true; spec: string | null } | { ok: false; token: string };

const DASHES = /[~～〜–—−‐-]/g;
const SEPARATORS = /[,，、;\s]+/;

/**
 * Reads a page list the way people type it: "1-3, 5", "1~3 5", "2–4".
 * Returns the server's form ("1-3,5"), `null` for an empty field, or the
 * first part that is not a page number. A range typed backwards ("5-3") is
 * turned around.
 */
export function parsePageSpec(input: string): PageSpecResult {
  const normalized = input.replace(DASHES, '-').replace(/\s*-\s*/g, '-').trim();
  if (normalized === '') return { ok: true, spec: null };
  const parts: string[] = [];
  for (const token of normalized.split(SEPARATORS)) {
    if (token === '') continue;
    const single = /^\d+$/.exec(token);
    const range = /^(\d+)-(\d+)$/.exec(token);
    if (single) {
      const page = Number(token);
      if (page < 1 || page > MAX_PAGE_NUMBER) return { ok: false, token };
      parts.push(String(page));
    } else if (range) {
      let start = Number(range[1]);
      let end = Number(range[2]);
      if (start > end) [start, end] = [end, start];
      if (start < 1 || end > MAX_PAGE_NUMBER) return { ok: false, token };
      parts.push(start === end ? String(start) : `${start}-${end}`);
    } else {
      return { ok: false, token };
    }
  }
  return { ok: true, spec: parts.length > 0 ? parts.join(',') : null };
}

/**
 * A typed number, clamped to `range`. Spaces and thousands commas are
 * ignored; anything that is not a whole number gives the default.
 */
export function normalizeCount(input: string, range: CountRange): number {
  const cleaned = input.replace(/[\s,]/g, '');
  if (!/^-?\d+$/.test(cleaned)) return range.fallback;
  const value = Number(cleaned);
  return Math.min(range.max, Math.max(range.min, value));
}

export type ParseOptionsResult = { ok: true; options: ParseOptions } | { ok: false; token: string };

/** The settings as POST /api/parse options, or the page-list part it cannot read. */
export function toParseOptions(settings: ExtractSettings): ParseOptionsResult {
  const pages = parsePageSpec(settings.pages);
  if (!pages.ok) return pages;
  return {
    ok: true,
    options: {
      pages: pages.spec,
      layout_detector: settings.findLayout ? 'heuristic' : 'none',
      include_comments: settings.includeComments,
      extract_crops: settings.extractCrops,
      include_chunks: settings.includeChunks,
      chunk_max_chars: normalizeCount(settings.chunkMaxChars, CHUNK_MAX_CHARS),
      chunk_overlap: normalizeCount(settings.chunkOverlap, CHUNK_OVERLAP),
    },
  };
}

/** What a read with these settings sends, as one comparable string. */
function readKey(settings: ExtractSettings): string {
  const result = toParseOptions(settings);
  if (!result.ok) return `invalid:${settings.pages.trim()}`;
  const { options } = result;
  return JSON.stringify([
    options.pages,
    options.layout_detector,
    options.include_comments,
    options.extract_crops,
    options.include_chunks,
    // Chunk sizes matter only when chunks are made.
    options.include_chunks ? options.chunk_max_chars : null,
    options.include_chunks ? options.chunk_overlap : null,
  ]);
}

/** Whether two settings would read a file the same way. */
export function sameReadSettings(a: ExtractSettings, b: ExtractSettings): boolean {
  return readKey(a) === readKey(b);
}

/** How many options differ from the defaults (shown on the 세부 설정 toggle). */
export function changedSettingsCount(settings: ExtractSettings): number {
  const pages = parsePageSpec(settings.pages);
  let changed = 0;
  if (!pages.ok || pages.spec !== null) changed += 1;
  if (settings.findLayout !== DEFAULT_SETTINGS.findLayout) changed += 1;
  if (settings.includeComments !== DEFAULT_SETTINGS.includeComments) changed += 1;
  if (settings.extractCrops !== DEFAULT_SETTINGS.extractCrops) changed += 1;
  if (settings.includeChunks !== DEFAULT_SETTINGS.includeChunks) changed += 1;
  if (normalizeCount(settings.chunkMaxChars, CHUNK_MAX_CHARS) !== CHUNK_MAX_CHARS.fallback) changed += 1;
  if (normalizeCount(settings.chunkOverlap, CHUNK_OVERLAP) !== CHUNK_OVERLAP.fallback) changed += 1;
  return changed;
}
