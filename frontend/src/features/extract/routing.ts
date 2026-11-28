/**
 * The routing plan in plain words: which pages were read how and why
 * ("1~2쪽은 글자가 든 PDF라 글자를 그대로 읽었어요"), and short count
 * summaries for the file list and the 근거 column.
 */
import type { ParseResult, RoutingPage } from './api.ts';
import { evidenceCopy, exportCopy, listCopy, routingCopy, unitCopy } from './copy.ts';
import { unitForFormat, type PageUnit } from './formats.ts';

/** Page lists longer than this many parts are cut short with "등 N쪽". */
const MAX_RANGE_PARTS = 6;

/** [1, 2, 4, 7, 8, 9] → ["1~2", "4", "7~9"]. Input order does not matter. */
export function compressPages(pages: readonly number[]): string[] {
  const sorted = [...new Set(pages)].filter((page) => page > 0).sort((a, b) => a - b);
  const parts: string[] = [];
  let index = 0;
  while (index < sorted.length) {
    const start = sorted[index] as number;
    let end = start;
    while (index + 1 < sorted.length && sorted[index + 1] === end + 1) {
      index += 1;
      end = sorted[index] as number;
    }
    parts.push(start === end ? `${start}` : `${start}~${end}`);
    index += 1;
  }
  return parts;
}

/** "1~2·4쪽", "슬라이드 3", "시트 1·3", or "1·3·5·7·9·11 등 20쪽" for long lists. */
export function pagesLabel(pages: readonly number[], unit: PageUnit): string {
  const parts = compressPages(pages);
  const copy = unitCopy[unit];
  if (parts.length > MAX_RANGE_PARTS) {
    const total = new Set(pages.filter((page) => page > 0)).size;
    return copy.many(parts.slice(0, MAX_RANGE_PARTS).join('·'), total);
  }
  return copy.range(parts.join('·'));
}

/** "2쪽", "슬라이드 2", "시트 2". */
export function whereLabel(page: number, unit: PageUnit): string {
  return unitCopy[unit].where(page);
}

/** "1쪽" or "1~2쪽" for something that spans pages. */
export function spanLabel(start: number, end: number, unit: PageUnit): string {
  if (end <= start) return whereLabel(start, unit);
  return unitCopy[unit].range(`${start}~${end}`);
}

const VISION_STRATEGIES = new Set(['embedded_image_ocr', 'vlm_region', 'vlm_full_page']);

/** Why the page was read this way, or `null` when there is nothing to add. */
function whyPhrase(category: string, format: string, unit: PageUnit): string | null {
  const noun = unitCopy[unit].noun;
  const isPdf = format === 'pdf';
  switch (category) {
    case 'digital_text':
      return isPdf ? routingCopy.why.digitalTextPdf : null;
    case 'multi_column':
      return routingCopy.why.multiColumn(noun);
    case 'table_heavy':
      return routingCopy.why.tableHeavy(noun);
    case 'scanned_image':
      return isPdf ? routingCopy.why.scannedPdf : routingCopy.why.imageOnly(noun);
    case 'hybrid_text_image':
      return routingCopy.why.hybrid(noun);
    case 'slide_like':
      return unit === 'slide' ? null : routingCopy.why.slideLike;
    case 'vector_diagram_heavy':
      return routingCopy.why.diagram(noun);
    case 'blank':
      return routingCopy.why.blank(noun);
    default:
      return null;
  }
}

/** How the page was read, as [sentence end, joining form]; the joining form is `null` when nothing can follow. */
function howPhrase(strategy: string, format: string, unit: PageUnit): readonly [string, string | null] {
  const isPdf = format === 'pdf';
  const how = routingCopy.how;
  switch (strategy) {
    case 'native_text':
      return how.nativeText;
    case 'multi_column_layout':
      return how.multiColumn;
    case 'ruled_table':
      return isPdf ? how.ruledTable : how.nativeTable;
    case 'horizontal_ruled_table':
      return isPdf ? how.horizontalTable : how.nativeTable;
    case 'borderless_table':
      return isPdf ? how.borderlessTable : how.nativeTable;
    case 'embedded_image_ocr':
    case 'vlm_region':
      return [routingCopy.visualRegion, null];
    case 'vlm_full_page':
      return [routingCopy.fullPage(unitCopy[unit].noun), null];
    case 'skip_decorative':
      return [routingCopy.skipped, null];
    default:
      return how.other;
  }
}

/** The words after "N쪽은" for one page. */
export function pagePhrase(page: RoutingPage, format: string, unit: PageUnit): string {
  const why = whyPhrase(page.category, format, unit);
  if (page.category === 'blank') return routingCopy.phrase(why, routingCopy.blankHow);
  const [end, joining] = howPhrase(page.strategy, format, unit);
  const addsVisuals = page.requiresVision && joining !== null && !VISION_STRATEGIES.has(page.strategy);
  return routingCopy.phrase(why, addsVisuals ? `${joining}, ${routingCopy.visualTail}` : end);
}

/**
 * One sentence per way of reading, pages grouped in the order they first
 * appear: ["1~2쪽은 글자가 든 PDF라 글자를 그대로 읽었어요", …].
 */
export function methodSentences(pages: readonly RoutingPage[], format: string): string[] {
  const unit = unitForFormat(format);
  const groups = new Map<string, number[]>();
  for (const page of pages) {
    const phrase = pagePhrase(page, format, unit);
    const group = groups.get(phrase);
    if (group) group.push(page.page);
    else groups.set(phrase, [page.page]);
  }
  return [...groups].map(([phrase, groupPages]) => routingCopy.sentence(pagesLabel(groupPages, unit), phrase));
}

/** "본문 2쪽 · 표 1개 · 그림·차트 1개 · AI용 문단 6개" for the 근거 column. */
export function foundSummary(result: ParseResult, includeChunks: boolean): string {
  const unit = unitForFormat(result.document.format);
  const copy = unitCopy[unit];
  const { analyzedPages, totalPages } = result.summary;
  const body =
    totalPages > analyzedPages
      ? evidenceCopy.partial(copy.body(analyzedPages), copy.ofTotal(totalPages))
      : copy.body(analyzedPages);
  const parts = [body, evidenceCopy.tables(result.tables.length), evidenceCopy.visuals(result.visuals.length)];
  if (includeChunks) parts.push(evidenceCopy.chunks(result.chunks.length));
  return parts.join(' · ');
}

/** "2쪽 · 표 1개" under a file name in the list. */
export function listMeta(result: ParseResult): string {
  const unit = unitForFormat(result.document.format);
  return `${unitCopy[unit].count(result.summary.analyzedPages)} · ${listCopy.tables(result.tables.length)}`;
}

/**
 * What the ZIP holds, for "ZIP 안: 본문 · 표(CSV·엑셀) · AI용 문단 · 처리 기록".
 * `extractCrops` and `includeChunks` are the settings the read used.
 */
export function zipContents(result: ParseResult, options: { extractCrops: boolean; includeChunks: boolean }): string[] {
  const parts: string[] = [exportCopy.parts.body];
  if (result.tables.length > 0) parts.push(exportCopy.parts.tables);
  const hasImages = result.visuals.some(
    (visual) => visual.cropStatus === 'saved' || visual.cropStatus === 'embedded_extracted',
  );
  if (options.extractCrops && hasImages) parts.push(exportCopy.parts.images);
  if (options.includeChunks) parts.push(exportCopy.parts.chunks);
  parts.push(exportCopy.parts.report);
  return parts;
}
