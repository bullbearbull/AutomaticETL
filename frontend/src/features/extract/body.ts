/**
 * Turns the server's document markdown into what the 본문 view shows.
 *
 * - Processing notes come out: page comments, "## Page N (표 중심 · …)"
 *   headings, the "> 출처:" line and figure notes. A small page marker
 *   ("2쪽") replaces each page break, and the 근거 column explains how each
 *   page was read.
 * - Each table and figure becomes its own part, so the screen shows a real
 *   table with a copy button and the figure's image.
 * - Headings start at h3 (# and ## → h3, ### → h4, …), so the document sits
 *   under the file name heading (h2).
 *
 * Lines inside fenced code blocks are never touched.
 */
import type { ExtractedTable, VisualRegion } from './api.ts';
import { unitCopy, visualCopy } from './copy.ts';
import type { PageUnit } from './formats.ts';
import { visualKindLabel } from './visuals.ts';

export type BodySegment =
  | { kind: 'text'; markdown: string }
  | { kind: 'page'; page: number }
  | { kind: 'table'; index: number; caption: string | null; continued: boolean }
  | { kind: 'visual'; index: number };

export interface BodyModel {
  segments: BodySegment[];
  /**
   * Markdown for [본문 복사하기]: the document without processing notes,
   * tables as markdown tables, figures as "[그림: caption]". Headings keep
   * their original levels.
   */
  copyText: string;
}

export interface BodyInput {
  markdown: string;
  tables: readonly Pick<ExtractedTable, 'markdown' | 'caption'>[];
  visuals: readonly Pick<VisualRegion, 'id' | 'kind' | 'caption'>[];
  unit: PageUnit;
  /** A title line equal to the file name is left out of the view; the heading above shows it already. */
  fileName?: string;
}

const PAGE_COMMENT = /^<!--\s*Page\s+(\d+)\b.*-->\s*$/;
const PAGE_HEADING = /^##\s+Page\s+(\d+)\b.*$/;
const HTML_COMMENT = /^\s*<!--.*-->\s*$/;
/** A figure the vision step already described: `<!-- p002_v01: figure_image via … -->`. */
const VISION_COMMENT = /^<!--\s*([^\s:]+):\s*\S+\s+via\b.*-->\s*$/;
/** A figure note: `> **[FIGURE_IMAGE · p002_v01]** 권장 전략: …`, followed by more `>` lines. */
const VISUAL_NOTE = /^>\s*\*\*\[[^\]·]*·\s*([^\]\s]+)\s*\]\*\*/;
const SOURCE_LINE = /^>\s*출처:/;
const SUB_LINE = /^\s*<sub>.*<\/sub>\s*$/;
const EMPTY_PAGE = /^\*\(추출된 본문 블록이 없는 페이지\)\*\s*$/;
const CONTINUED_NOTE = /^\*\(이전 페이지 표 `[^`]*`에서 이어짐\)\*\s*$/;
const FENCE_OPEN = /^ {0,3}(`{3,}|~{3,})/;
const HEADING = /^( {0,3})(#{1,6})(?=[ \t]|$)/;
const TITLE = /^#[ \t]+(.*?)[ \t]*$/;

function splitLines(text: string): string[] {
  return text.replace(/\r\n?/g, '\n').split('\n');
}

/** Joins lines, drops blank lines at both ends and squeezes runs of blank lines. */
function tidy(lines: readonly string[]): string {
  return lines
    .join('\n')
    .replace(/^(?:[ \t]*\n)+/, '')
    .replace(/\n(?:[ \t]*\n){2,}/g, '\n\n')
    .trimEnd();
}

/** Whether a table's markdown starts with the "이전 페이지 표에서 이어짐" note. */
export function isContinuedTable(markdown: string): boolean {
  return CONTINUED_NOTE.test(splitLines(markdown)[0] ?? '');
}

/** # → ###, ## → ###, ### → ####, … up to ######. */
export function shiftHeading(line: string): string {
  const match = HEADING.exec(line);
  if (!match) return line;
  const level = (match[2] as string).length;
  const next = Math.min(6, Math.max(3, level + 1));
  return `${match[1]}${'#'.repeat(next)}${line.slice(match[0].length)}`;
}

function closesFence(line: string, fence: { char: string; size: number }): boolean {
  const match = /^ {0,3}(`{3,}|~{3,})\s*$/.exec(line);
  const marker = match?.[1];
  return marker !== undefined && marker[0] === fence.char && marker.length >= fence.size;
}

/** Whether the next non-blank line starts a new page (or there is none). */
function endsPage(lines: readonly string[], index: number): boolean {
  for (let next = index + 1; next < lines.length; next += 1) {
    const line = lines[next] as string;
    if (line.trim() === '') continue;
    return PAGE_COMMENT.test(line) || PAGE_HEADING.test(line);
  }
  return true;
}

export function buildBody(input: BodyInput): BodyModel {
  const lines = splitLines(input.markdown);
  const tableLines = input.tables.map((table) => splitLines(table.markdown));
  const tablesByFirstLine = new Map<string, number[]>();
  tableLines.forEach((tableText, index) => {
    const first = tableText[0];
    if (first === undefined || first.trim() === '') return;
    const known = tablesByFirstLine.get(first);
    if (known) known.push(index);
    else tablesByFirstLine.set(first, [index]);
  });
  const visualIndex = new Map<string, number>();
  input.visuals.forEach((visual, index) => {
    if (!visualIndex.has(visual.id)) visualIndex.set(visual.id, index);
  });
  const usedTables = new Set<number>();
  const usedVisuals = new Set<number>();

  const segments: BodySegment[] = [];
  const copy: string[] = [];
  let text: string[] = [];
  let fence: { char: string; size: number } | null = null;
  let pageSeen = false;

  const flush = () => {
    const markdown = tidy(text);
    if (markdown !== '') segments.push({ kind: 'text', markdown });
    text = [];
  };

  const pageBreak = (page: number) => {
    // No marker before the first page: the document starts there.
    if (pageSeen) {
      flush();
      segments.push({ kind: 'page', page });
    } else {
      text.push('');
    }
    pageSeen = true;
    copy.push('');
  };

  const addVisual = (id: string) => {
    const index = visualIndex.get(id);
    if (index === undefined || usedVisuals.has(index)) return;
    usedVisuals.add(index);
    flush();
    segments.push({ kind: 'visual', index });
    const visual = input.visuals[index];
    if (visual) copy.push('', visualCopy.placeholder(visualKindLabel(visual.kind), visual.caption), '');
  };

  /** The table whose markdown starts at `index` as a block of its own, if any. */
  const matchTable = (index: number): number | null => {
    const candidates = tablesByFirstLine.get(lines[index] as string);
    if (!candidates) return null;
    if (index > 0 && (lines[index - 1] as string).trim() !== '') return null;
    for (const candidate of candidates) {
      if (usedTables.has(candidate)) continue;
      const tableText = tableLines[candidate] as string[];
      if (index + tableText.length > lines.length) continue;
      if (!tableText.every((tableLine, offset) => lines[index + offset] === tableLine)) continue;
      const after = lines[index + tableText.length];
      if (after !== undefined && after.trim() !== '') continue;
      return candidate;
    }
    return null;
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index] as string;

    if (fence) {
      text.push(line);
      copy.push(line);
      if (closesFence(line, fence)) fence = null;
      continue;
    }
    const open = FENCE_OPEN.exec(line);
    if (open) {
      const marker = open[1] as string;
      fence = { char: marker[0] as string, size: marker.length };
      text.push(line);
      copy.push(line);
      continue;
    }

    const pageComment = PAGE_COMMENT.exec(line);
    if (pageComment) {
      if (PAGE_HEADING.test(lines[index + 1] ?? '')) index += 1;
      pageBreak(Number(pageComment[1]));
      continue;
    }
    const pageHeading = PAGE_HEADING.exec(line);
    if (pageHeading) {
      pageBreak(Number(pageHeading[1]));
      continue;
    }
    if (!pageSeen && SOURCE_LINE.test(line)) continue;
    if (line.trim() === '---' && endsPage(lines, index)) continue;
    if (EMPTY_PAGE.test(line)) {
      text.push(`*${unitCopy[input.unit].noText}*`);
      continue;
    }

    const visionComment = VISION_COMMENT.exec(line);
    if (visionComment) {
      addVisual(visionComment[1] as string);
      continue;
    }
    if (HTML_COMMENT.test(line) || SUB_LINE.test(line)) continue;
    const visualNote = VISUAL_NOTE.exec(line);
    if (visualNote) {
      while (index + 1 < lines.length && (lines[index + 1] as string).startsWith('>')) index += 1;
      addVisual(visualNote[1] as string);
      continue;
    }

    const table = matchTable(index);
    if (table !== null) {
      const tableText = tableLines[table] as string[];
      usedTables.add(table);
      flush();
      segments.push({
        kind: 'table',
        index: table,
        caption: input.tables[table]?.caption ?? null,
        continued: isContinuedTable(input.tables[table]?.markdown ?? ''),
      });
      for (const tableLine of tableText) copy.push(tableLine);
      index += tableText.length - 1;
      continue;
    }

    if (!pageSeen && input.fileName !== undefined && TITLE.exec(line)?.[1] === input.fileName) {
      copy.push(line);
      continue;
    }
    text.push(shiftHeading(line));
    copy.push(line);
  }
  flush();

  return { segments, copyText: tidy(copy) };
}
