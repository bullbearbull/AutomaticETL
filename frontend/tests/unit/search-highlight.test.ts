import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  analyzeHitMatch,
  collectHighlightTerms,
  extractSurroundingPassage,
  filterDisplayMatchedTerms,
  formatCitationCopyText,
  partitionHitsByRelevance,
  RELATIVE_SCORE_RATIO,
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

  // Never slices inside a multi-word matched phrase at the end word boundary.
  const longSingle = `${'가'.repeat(200)}보관 기간 연장${'나'.repeat(200)}`;
  const longWindow = extractSurroundingPassage(longSingle, ['보관 기간 연장', '보관 기간'], {
    maxChars: 280,
    contextSentences: 1,
  });
  assert.match(longWindow.passage, /보관 기간 연장/);
});

test('partitionHitsByRelevance handles real-engine score distributions and filters 2-gram fragments', () => {
  // Real 6-doc corpus distribution for "보관 기간": exact body match (0.2333) stays primary, partial (0.0) folds.
  const storageHits = [
    {
      id: 'edu',
      title: '제4조 교육 이수 기록의 보관 기간',
      sectionPath: ['제4조 교육 이수 기록의 보관 기간'],
      fullText: '보관 기간은 5년이다.',
      matchedTerms: ['보관', '기간'],
      rerankScore: null,
      finalScore: 0.6065,
    },
    {
      id: 'sec',
      title: '제9조 접속 기록의 보관 기간',
      sectionPath: ['제9조 접속 기록의 보관 기간'],
      fullText: '보관 기간은 최소 3년으로 정한다.',
      matchedTerms: ['보관', '기간'],
      rerankScore: null,
      finalScore: 0.6065,
    },
    {
      id: 'receipt',
      title: '제3조 지출 증빙 관리',
      sectionPath: ['제3조 지출 증빙 관리'],
      fullText: '영수증 보관 기간은 3년으로 정한다.',
      matchedTerms: ['보관', '기간'],
      rerankScore: null,
      finalScore: 0.2333,
    },
    {
      id: 'club',
      title: '제5조 사내 동호회 활동비 지원',
      sectionPath: ['제5조 사내 동호회 활동비 지원'],
      fullText: '영수증 증빙은 참고용으로 잠깐 보관한다.',
      matchedTerms: ['보관'],
      rerankScore: null,
      finalScore: 0.0,
    },
  ];
  const partitionedStorage = partitionHitsByRelevance(storageHits, '보관 기간');
  assert.deepEqual(
    partitionedStorage.primary.map((h) => h.id),
    ['edu', 'sec', 'receipt'],
  );
  assert.deepEqual(
    partitionedStorage.lowRelevance.map((h) => h.id),
    ['club'],
  );

  // Real 6-doc corpus distribution for "비밀번호 승인": 2-gram-only "번호" (0.5469) folds.
  const pwHits = [
    {
      id: 'pw',
      title: '제12조 비밀번호 관리',
      sectionPath: ['제12조 비밀번호 관리'],
      fullText: '사용자 비밀번호는 90일마다 변경해야 한다.',
      matchedTerms: ['비밀번호', '비밀', '밀번', '번호'],
      rerankScore: null,
      finalScore: 0.8887,
    },
    {
      id: 'approval',
      title: '제6조 결재 승인 절차',
      sectionPath: ['제6조 결재 승인 절차'],
      fullText: '예산 집행 전 팀장 승인을 받아야 한다.',
      matchedTerms: ['승인'],
      rerankScore: null,
      finalScore: 0.6262,
    },
    {
      id: 'tel',
      title: '제2조 내선 번호 안내',
      sectionPath: ['제2조 내선 번호 안내'],
      fullText: '부서별 대표 내선 번호는 사내 포털에서 확인한다.',
      matchedTerms: ['번호'],
      rerankScore: null,
      finalScore: 0.5469,
    },
  ];
  const partitionedPw = partitionHitsByRelevance(pwHits, '비밀번호 승인');
  assert.deepEqual(
    partitionedPw.primary.map((h) => h.id),
    ['pw', 'approval'],
  );
  assert.deepEqual(
    partitionedPw.lowRelevance.map((h) => h.id),
    ['tel'],
  );
  assert.equal(analyzeHitMatch(pwHits[2]!, '비밀번호 승인').kind, 'fragment');
  assert.deepEqual(filterDisplayMatchedTerms('비밀번호 승인', pwHits[0]!.matchedTerms), ['비밀번호']);

  // Synonym hit stays primary when score meets ratio threshold and classifies as 'synonym'.
  const synHit = {
    id: 'syn',
    title: '제9조 접속 기록의 보관 기간',
    sectionPath: ['제9조 접속 기록의 보관 기간'],
    fullText: '보관 기간은 최소 3년으로 정한다.',
    matchedTerms: ['보관'],
    rerankScore: null,
    finalScore: 0.6,
  };
  assert.equal(analyzeHitMatch(synHit, '보존').kind, 'synonym');
  assert.deepEqual(partitionHitsByRelevance([synHit], '보존').primary.map((h) => h.id), ['syn']);

  // Score-ratio fallback when query is omitted.
  const ratioOnly = partitionHitsByRelevance([
    { id: 'top', finalScore: 0.8 },
    { id: 'mid', finalScore: 0.8 * RELATIVE_SCORE_RATIO },
    { id: 'low', finalScore: 0.2 },
  ]);
  assert.deepEqual(
    ratioOnly.primary.map((h) => h.id),
    ['top', 'mid'],
  );
  assert.deepEqual(
    ratioOnly.lowRelevance.map((h) => h.id),
    ['low'],
  );
});

test('analyzeHitMatch and partitionHitsByRelevance handle compound queries without merging separate words or fields', () => {
  const compoundHits = [
    {
      id: 'top',
      title: '제4조 보관 기간',
      sectionPath: ['제4조 보관 기간'],
      fullText: '보관 기간은 5년이다.',
      matchedTerms: ['보관', '기간'],
      rerankScore: null,
      finalScore: 0.568,
    },
    {
      id: 'spaced-body',
      title: '제3조 지출 증빙',
      sectionPath: ['제3조 지출 증빙'],
      fullText: '영수증 보관 기간은 3년으로 정한다.',
      matchedTerms: ['보관', '기간'],
      rerankScore: null,
      finalScore: 0.377,
    },
    {
      id: 'partial-only',
      title: '제5조 영수증 증빙',
      sectionPath: ['제5조 영수증 증빙'],
      fullText: '증빙을 잠시 보관한다.',
      matchedTerms: ['보관'],
      rerankScore: null,
      finalScore: 0.19,
    },
  ];
  const compoundMatch = analyzeHitMatch(compoundHits[1]!, '보관기간');
  assert.equal(compoundMatch.kind, 'all_words');
  assert.equal(compoundMatch.matchedWordCount, 1);

  const partitionedCompound = partitionHitsByRelevance(compoundHits, '보관기간');
  assert.deepEqual(
    partitionedCompound.primary.map((h) => h.id),
    ['top', 'spaced-body'],
  );
  assert.deepEqual(
    partitionedCompound.lowRelevance.map((h) => h.id),
    ['partial-only'],
  );

  // Separated words like "비밀 문서 … 번호" still classify as fragment, not compound "비밀번호".
  const separatedHit = {
    id: 'sep',
    title: '문서 관리',
    sectionPath: ['문서 관리'],
    fullText: '비밀 문서는 별도로 분류해 관리 번호를 붙인다.',
    matchedTerms: ['비밀', '번호'],
    rerankScore: null,
    finalScore: 0.35,
  };
  assert.equal(analyzeHitMatch(separatedHit, '비밀번호').kind, 'fragment');

  // Words separated by title/fullText boundary must never glue together either.
  const crossFieldHit = {
    id: 'cross',
    title: '사내 비밀',
    sectionPath: ['사내 비밀'],
    fullText: '번호 안내 목록이다.',
    matchedTerms: ['비밀', '번호'],
    rerankScore: null,
    finalScore: 0.35,
  };
  assert.equal(analyzeHitMatch(crossFieldHit, '비밀번호').kind, 'fragment');

  const partitionedSep = partitionHitsByRelevance(
    [
      {
        id: 'exact-pw',
        title: '비밀번호 관리',
        sectionPath: ['비밀번호 관리'],
        fullText: '비밀번호는 주기적으로 바꾼다.',
        matchedTerms: ['비밀번호'],
        rerankScore: null,
        finalScore: 0.85,
      },
      separatedHit,
      crossFieldHit,
    ],
    '비밀번호',
  );
  assert.deepEqual(
    partitionedSep.primary.map((h) => h.id),
    ['exact-pw'],
  );
  assert.deepEqual(
    partitionedSep.lowRelevance.map((h) => h.id),
    ['sep', 'cross'],
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
});
