import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  collectHighlightTerms,
  extractSurroundingPassage,
  formatCitationCopyText,
  LOW_RELEVANCE_THRESHOLD,
  partitionHitsByRelevance,
  relevancePercent,
  splitTextWithHighlights,
} from '../../src/features/search/highlight.ts';

test('collectHighlightTerms and splitTextWithHighlights highlight longest terms first without empty loops', () => {
  const terms = collectHighlightTerms('보관 기간', ['보관', '기간']);
  assert.equal(terms[0], '보관 기간');

  const segments = splitTextWithHighlights('서류의 보관 기간은 5년이며 보관 장소는 본사다.', terms);
  assert.deepEqual(
    segments.filter((s) => s.match).map((s) => s.text),
    ['보관 기간', '보관'],
  );

  // Regex special characters never break highlighting.
  const regexSafe = splitTextWithHighlights('가격 (VAT+10%) 확인', ['(VAT+10%)']);
  assert.deepEqual(
    regexSafe.filter((s) => s.match).map((s) => s.text),
    ['(VAT+10%)'],
  );
});

test('extractSurroundingPassage keeps surrounding sentences before and after the hit', () => {
  const fullText = [
    '이 규정은 회사 문서 관리의 기본 원칙을 정한다.',
    '모든 부서는 문서를 분류해 등록해야 한다.',
    '특히 계약서 원본의 보관 기간은 체결일로부터 5년으로 한다.',
    '기한이 지난 문서는 승인 절차를 거쳐 파기한다.',
    '세부 양식은 별표를 따른다.',
  ].join(' ');

  const res = extractSurroundingPassage(fullText, ['보관 기간'], {
    maxChars: 140,
    contextSentences: 1,
  });
  assert.match(res.passage, /모든 부서는/);
  assert.match(res.passage, /보관 기간은 체결일로부터 5년/);
  assert.match(res.passage, /기한이 지난 문서는/);
  assert.equal(res.truncatedBefore, true);
  assert.equal(res.truncatedAfter, true);
});

test('partitionHitsByRelevance folds low-relevance hits while always keeping the top hit visible', () => {
  const { primary, lowRelevance } = partitionHitsByRelevance([
    { id: '1', finalScore: 0.82 },
    { id: '2', finalScore: 0.45 },
    { id: '3', finalScore: LOW_RELEVANCE_THRESHOLD - 0.08 },
    { id: '4', finalScore: 0.12 },
  ]);
  assert.deepEqual(
    primary.map((h) => h.id),
    ['1', '2'],
  );
  assert.deepEqual(
    lowRelevance.map((h) => h.id),
    ['3', '4'],
  );

  // Even when all scores are below 0.30, the top hit stays primary and weaker tail folds.
  const lowTop = partitionHitsByRelevance([
    { id: 'a', finalScore: 0.2 },
    { id: 'b', finalScore: 0.19 },
    { id: 'c', finalScore: 0.1 },
  ]);
  assert.deepEqual(
    lowTop.primary.map((h) => h.id),
    ['a', 'b'],
  );
  assert.deepEqual(
    lowTop.lowRelevance.map((h) => h.id),
    ['c'],
  );
});

test('formatCitationCopyText includes document name, page location, section title, and passage', () => {
  assert.equal(
    formatCitationCopyText({
      fileName: '인사규정.pdf',
      pageLabel: '4쪽',
      sectionTitle: '제2장 › 보관 기간',
      passage: '  보관 기간은 5년이다.  ',
    }),
    '[인사규정.pdf · 4쪽 · 제2장 › 보관 기간]\n보관 기간은 5년이다.',
  );
  assert.equal(relevancePercent(0.754), 75);
  assert.equal(relevancePercent(0), 1);
});
