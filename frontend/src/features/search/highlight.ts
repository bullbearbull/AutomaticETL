import type { EvidenceHitInfo } from './api.ts';

/** Score ratio vs. the top hit below which partial matches are folded behind `<Details>`. */
export const RELATIVE_SCORE_RATIO = 0.55;

const QUERY_WORD_RE = /[0-9A-Za-z가-힣]+(?:[-_./][0-9A-Za-z가-힣]+)*/g;

export type HitMatchKind = 'all_words' | 'partial_words' | 'synonym' | 'fragment' | 'semantic';

export interface HitMatchInfo {
  kind: HitMatchKind;
  matchedWordCount: number;
  queryWordCount: number;
  displayTerms: string[];
}

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
    let hitEnd = matchedUnit.length;
    for (const term of lowerTerms) {
      const idx = lowerUnit.indexOf(term);
      if (idx >= 0 && (idx < hitOffset || (idx === hitOffset && idx + term.length > hitEnd))) {
        hitOffset = idx;
        hitEnd = idx + term.length;
      }
    }
    const half = Math.floor(maxChars / 2);
    let start = Math.max(0, hitOffset - half);
    let end = Math.min(matchedUnit.length, Math.max(start + maxChars, hitEnd));
    if (end - start < maxChars && start > 0) {
      start = Math.min(hitOffset, Math.max(0, end - maxChars));
    }
    if (start > 0) {
      const nextSpace = matchedUnit.indexOf(' ', start);
      if (nextSpace >= 0 && nextSpace < hitOffset && end - nextSpace > 60) {
        start = nextSpace + 1;
      }
    }
    if (end < matchedUnit.length) {
      const prevSpace = matchedUnit.lastIndexOf(' ', end);
      if (prevSpace >= hitEnd) {
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

/** Extracts unique lowercase query words matching the backend FTS5 tokenizer boundaries. */
export function extractQueryWords(query: string): string[] {
  const matches = query.toLowerCase().match(QUERY_WORD_RE);
  if (!matches) return [];
  const seen = new Set<string>();
  const words: string[] = [];
  for (const w of matches) {
    if (!seen.has(w)) {
      seen.add(w);
      words.push(w);
    }
  }
  return words;
}

/**
 * Drops redundant internal 2-gram fragments from backend `matchedTerms` when
 * a longer containing matched term (e.g. `비밀번호` vs `비밀`, `밀번`, `번호`) is present.
 */
export function filterDisplayMatchedTerms(_query: string, matchedTerms: readonly string[]): string[] {
  const seen = new Set<string>();
  const unique: string[] = [];
  for (const raw of matchedTerms) {
    const trimmed = raw.trim();
    if (!trimmed) continue;
    const lower = trimmed.toLowerCase();
    if (!seen.has(lower)) {
      seen.add(lower);
      unique.push(trimmed);
    }
  }
  return unique.filter((term, idx) => {
    const low = term.toLowerCase();
    return !unique.some(
      (other, otherIdx) =>
        otherIdx !== idx && other.length > term.length && other.toLowerCase().includes(low),
    );
  });
}

/**
 * Classifies how well a hit matches the user's query using actual word coverage
 * and cleaned matched terms instead of mapping raw BM25 blends to percentages.
 */
export function analyzeHitMatch(
  hit: Pick<EvidenceHitInfo, 'title' | 'sectionPath' | 'fullText' | 'matchedTerms' | 'rerankScore'>,
  query: string,
): HitMatchInfo {
  const queryWords = extractQueryWords(query);
  const haystack = `${hit.title} ${hit.sectionPath.join(' ')} ${hit.fullText}`.toLowerCase();
  const compactHaystack = [hit.title, ...hit.sectionPath, hit.fullText]
    .map((part) => part.toLowerCase().replace(/\s+/g, ''))
    .join('\0');
  const matchedWordCount = queryWords.filter(
    (w) => haystack.includes(w) || compactHaystack.includes(w),
  ).length;
  const displayTerms = filterDisplayMatchedTerms(query, hit.matchedTerms);

  const isSubFragment = (term: string): boolean => {
    const low = term.toLowerCase();
    return queryWords.some((w) => w.length > low.length && w.includes(low));
  };
  const queryWordSet = new Set(queryWords);
  const synonymCount = displayTerms.filter(
    (term) => !queryWordSet.has(term.toLowerCase()) && !isSubFragment(term),
  ).length;

  let kind: HitMatchKind;
  if (queryWords.length > 0 && matchedWordCount === queryWords.length) {
    kind = 'all_words';
  } else if (matchedWordCount > 0) {
    kind = 'partial_words';
  } else if (synonymCount > 0) {
    kind = 'synonym';
  } else if (hit.rerankScore !== null) {
    kind = 'semantic';
  } else if (displayTerms.length > 0) {
    kind = 'fragment';
  } else {
    kind = 'semantic';
  }

  return {
    kind,
    matchedWordCount,
    queryWordCount: queryWords.length,
    displayTerms,
  };
}

/**
 * Splits hits into high-relevance results shown right away and low-relevance
 * results folded behind `<Details>`. Uses full query-word coverage and score
 * ratio against the top hit rather than an absolute BM25 percentage cutoff.
 */
export function partitionHitsByRelevance<
  T extends Pick<EvidenceHitInfo, 'finalScore'> &
    Partial<Pick<EvidenceHitInfo, 'title' | 'sectionPath' | 'fullText' | 'matchedTerms' | 'rerankScore'>>,
>(hits: readonly T[], query = ''): { primary: T[]; lowRelevance: T[] } {
  if (hits.length <= 1) {
    return { primary: [...hits], lowRelevance: [] };
  }
  const topScore = hits[0]!.finalScore;
  const queryWords = extractQueryWords(query);

  const primary: T[] = [];
  const lowRelevance: T[] = [];
  hits.forEach((hit, index) => {
    if (index === 0) {
      primary.push(hit);
      return;
    }
    if (queryWords.length > 0 && typeof hit.fullText === 'string') {
      const info = analyzeHitMatch(
        {
          title: hit.title ?? '',
          sectionPath: hit.sectionPath ?? [],
          fullText: hit.fullText,
          matchedTerms: hit.matchedTerms ?? [],
          rerankScore: hit.rerankScore ?? null,
        },
        query,
      );
      if (info.kind === 'fragment' && (hit.rerankScore ?? null) === null) {
        lowRelevance.push(hit);
        return;
      }
      if (info.kind === 'all_words') {
        primary.push(hit);
        return;
      }
    }
    if (topScore <= 0 || hit.finalScore >= topScore * RELATIVE_SCORE_RATIO) {
      primary.push(hit);
    } else {
      lowRelevance.push(hit);
    }
  });
  return { primary, lowRelevance };
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
