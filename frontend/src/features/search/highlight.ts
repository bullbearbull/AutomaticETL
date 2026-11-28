import type { EvidenceHitInfo } from './api.ts';

/** Hits below this final score are folded under "관련도가 낮은 결과 N개 더 보기". */
export const LOW_RELEVANCE_THRESHOLD = 0.30;

export interface HighlightSegment {
  text: string;
  match: boolean;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Builds the ordered list of unique search terms to highlight (`matchedTerms`
 * plus query tokens and full trimmed phrase), sorted longest-first so compound
 * terms take precedence over short substrings.
 */
export function collectHighlightTerms(query: string, matchedTerms: readonly string[]): string[] {
  const seen = new Map<string, string>();
  const add = (term: string) => {
    const trimmed = term.trim();
    if (!trimmed) return;
    const lower = trimmed.toLowerCase();
    if (!seen.has(lower)) seen.set(lower, trimmed);
  };

  const trimmedQuery = query.trim();
  if (trimmedQuery) add(trimmedQuery);
  for (const t of matchedTerms) add(t);

  const tokens = trimmedQuery.split(/\s+/).filter(Boolean);
  const longTokens = tokens.filter((t) => t.length >= 2);
  for (const t of longTokens.length > 0 ? longTokens : tokens) {
    add(t);
  }

  return [...seen.values()].sort((a, b) => b.length - a.length);
}

/**
 * Splits `text` into matched and unmatched segments for accessible
 * bold+underline highlighting without color or HTML injection.
 */
export function splitTextWithHighlights(text: string, terms: readonly string[]): HighlightSegment[] {
  if (!text) return [];
  const cleanTerms = [...new Set(terms.map((t) => t.trim()).filter((t) => t.length > 0))];
  if (cleanTerms.length === 0) return [{ text, match: false }];

  const sorted = [...cleanTerms].sort((a, b) => b.length - a.length);
  const regex = new RegExp(`(${sorted.map(escapeRegExp).join('|')})`, 'gi');
  const segments: HighlightSegment[] = [];
  let cursor = 0;

  for (const match of text.matchAll(regex)) {
    const idx = match.index ?? 0;
    const value = match[0] ?? '';
    if (!value) continue;
    if (idx > cursor) {
      segments.push({ text: text.slice(cursor, idx), match: false });
    }
    segments.push({ text: value, match: true });
    cursor = idx + value.length;
  }

  if (cursor < text.length) {
    segments.push({ text: text.slice(cursor), match: false });
  }
  return segments.length > 0 ? segments : [{ text, match: false }];
}

function splitSentenceUnits(text: string): string[] {
  const units: string[] = [];
  for (const line of text.split(/\n+/)) {
    const cleanLine = line.trim();
    if (!cleanLine) continue;
    // Split on sentence-ending punctuation followed by whitespace while keeping delimiters.
    const sentences = cleanLine.split(/(?<=[.!?。])\s+/);
    for (const s of sentences) {
      const trimmed = s.trim();
      if (trimmed) units.push(trimmed);
    }
  }
  return units;
}

export interface SurroundingPassage {
  passage: string;
  truncatedBefore: boolean;
  truncatedAfter: boolean;
}

/**
 * Extracts a passage containing the first match along with surrounding sentences
 * before and after it, bounded to `maxChars`. Falls back to a word-boundary window
 * on long single-sentence texts or to the opening text if matches were only in headings.
 */
export function extractSurroundingPassage(
  fullText: string,
  terms: readonly string[],
  options: { maxChars?: number; contextSentences?: number } = {},
): SurroundingPassage {
  const maxChars = options.maxChars ?? 300;
  const contextSentences = options.contextSentences ?? 1;
  const normalized = fullText.trim();
  if (!normalized) {
    return { passage: '', truncatedBefore: false, truncatedAfter: false };
  }

  const lowerTerms = terms.map((t) => t.trim().toLowerCase()).filter(Boolean);
  const units = splitSentenceUnits(normalized);
  if (units.length <= 1 && normalized.length <= maxChars) {
    return { passage: normalized, truncatedBefore: false, truncatedAfter: false };
  }

  let matchIndex = -1;
  if (lowerTerms.length > 0) {
    matchIndex = units.findIndex((unit) => {
      const lowerUnit = unit.toLowerCase();
      return lowerTerms.some((term) => lowerUnit.includes(term));
    });
  }

  if (matchIndex < 0) {
    // No term inside body (e.g. matched heading or synonym): return opening window cleanly.
    if (normalized.length <= maxChars) {
      return { passage: normalized, truncatedBefore: false, truncatedAfter: false };
    }
    const slice = normalized.slice(0, maxChars).trimEnd();
    return { passage: `${slice}…`, truncatedBefore: false, truncatedAfter: true };
  }

  // If the matched single unit itself exceeds maxChars, window cleanly around the first match offset.
  const matchedUnit = units[matchIndex]!;
  if (matchedUnit.length > maxChars) {
    const lowerUnit = matchedUnit.toLowerCase();
    let hitOffset = matchedUnit.length;
    for (const term of lowerTerms) {
      const idx = lowerUnit.indexOf(term);
      if (idx >= 0 && idx < hitOffset) hitOffset = idx;
    }
    const half = Math.floor(maxChars / 2);
    let start = Math.max(0, hitOffset - half);
    let end = Math.min(matchedUnit.length, start + maxChars);
    if (end - start < maxChars && start > 0) {
      start = Math.max(0, end - maxChars);
    }
    if (start > 0) {
      const nextSpace = matchedUnit.indexOf(' ', start);
      if (nextSpace >= 0 && nextSpace < hitOffset && end - nextSpace > 60) {
        start = nextSpace + 1;
      }
    }
    if (end < matchedUnit.length) {
      const prevSpace = matchedUnit.lastIndexOf(' ', end);
      if (prevSpace > hitOffset) {
        end = prevSpace;
      }
    }
    const before = start > 0 || matchIndex > 0;
    const after = end < matchedUnit.length || matchIndex < units.length - 1;
    const body = matchedUnit.slice(start, end).trim();
    return {
      passage: `${before ? '…' : ''}${body}${after ? '…' : ''}`,
      truncatedBefore: before,
      truncatedAfter: after,
    };
  }

  let startIdx = Math.max(0, matchIndex - contextSentences);
  let endIdx = Math.min(units.length - 1, matchIndex + contextSentences);

  // Trim outer context sentences if total exceeds maxChars, always keeping matchIndex.
  while (units.slice(startIdx, endIdx + 1).join(' ').length > maxChars && (startIdx < matchIndex || endIdx > matchIndex)) {
    if (endIdx - matchIndex > matchIndex - startIdx || startIdx === matchIndex) {
      endIdx -= 1;
    } else {
      startIdx += 1;
    }
  }

  const truncatedBefore = startIdx > 0;
  const truncatedAfter = endIdx < units.length - 1;
  const body = units.slice(startIdx, endIdx + 1).join(' ');
  return {
    passage: `${truncatedBefore ? '… ' : ''}${body}${truncatedAfter ? ' …' : ''}`,
    truncatedBefore,
    truncatedAfter,
  };
}

/**
 * Splits hits into high-relevance results shown right away and low-relevance
 * results folded behind `<Details>`. Guarantees at least the top hit stays visible.
 */
export function partitionHitsByRelevance<T extends Pick<EvidenceHitInfo, 'finalScore'>>(
  hits: readonly T[],
): { primary: T[]; lowRelevance: T[] } {
  if (hits.length <= 1) {
    return { primary: [...hits], lowRelevance: [] };
  }
  const topScore = hits[0]!.finalScore;
  const cutoff = topScore >= LOW_RELEVANCE_THRESHOLD ? LOW_RELEVANCE_THRESHOLD : topScore * 0.85;

  const primary: T[] = [];
  const lowRelevance: T[] = [];
  hits.forEach((hit, index) => {
    if (index === 0 || hit.finalScore >= cutoff) {
      primary.push(hit);
    } else {
      lowRelevance.push(hit);
    }
  });
  return { primary, lowRelevance };
}

/** Clamps final score into a readable 1–100 integer percentage. */
export function relevancePercent(finalScore: number): number {
  if (!Number.isFinite(finalScore)) return 1;
  return Math.max(1, Math.min(100, Math.round(finalScore * 100)));
}

/** Formats copied evidence text with document name, page location, section heading, and passage. */
export function formatCitationCopyText(input: {
  fileName: string;
  pageLabel: string;
  sectionTitle: string;
  passage: string;
}): string {
  const header = `[${input.fileName} · ${input.pageLabel} · ${input.sectionTitle}]`;
  return `${header}\n${input.passage.trim()}`;
}
