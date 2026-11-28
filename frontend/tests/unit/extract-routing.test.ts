import assert from 'node:assert/strict';
import { test } from 'node:test';
import { normalizeParseResponse, type RoutingPage } from '../../src/features/extract/api.ts';
import {
  compressPages,
  foundSummary,
  listMeta,
  methodSentences,
  pagesLabel,
  spanLabel,
  whereLabel,
  zipContents,
} from '../../src/features/extract/routing.ts';
import { sampleRaw } from './extract-sample.ts';

function page(number: number, category: string, strategy: string, requiresVision = false): RoutingPage {
  return { page: number, category, strategy, requiresVision };
}

test('page numbers are grouped into ranges', () => {
  assert.deepEqual(compressPages([1, 2, 4, 7, 8, 9]), ['1~2', '4', '7~9']);
  assert.deepEqual(compressPages([9, 8, 7, 1]), ['1', '7~9']);
  assert.deepEqual(compressPages([3, 3, 0]), ['3']);
  assert.deepEqual(compressPages([]), []);
});

test('page labels follow each format’s unit', () => {
  assert.equal(pagesLabel([1, 2, 4], 'page'), '1~2·4쪽');
  assert.equal(pagesLabel([3], 'slide'), '슬라이드 3');
  assert.equal(pagesLabel([1, 3], 'sheet'), '시트 1·3');
  assert.equal(whereLabel(2, 'page'), '2쪽');
  assert.equal(spanLabel(1, 1, 'page'), '1쪽');
  assert.equal(spanLabel(1, 2, 'slide'), '슬라이드 1~2');
});

test('long page lists are cut short with a total', () => {
  const odd = Array.from({ length: 20 }, (_, index) => index * 2 + 1);
  assert.equal(pagesLabel(odd, 'page'), '1·3·5·7·9·11 등 20쪽');
  assert.equal(pagesLabel(odd, 'slide'), '슬라이드 1·3·5·7·9·11 등 20장');
});

test('a text PDF reads as the plain example sentence', () => {
  const pages = [page(1, 'digital_text', 'native_text'), page(2, 'digital_text', 'native_text')];
  assert.deepEqual(methodSentences(pages, 'pdf'), ['1~2쪽은 글자가 든 PDF라 글자를 그대로 읽었어요']);
});

test('pages read the same way share one sentence, in first-seen order', () => {
  const pages = [
    page(1, 'table_heavy', 'ruled_table'),
    page(2, 'digital_text', 'native_text'),
    page(3, 'table_heavy', 'ruled_table'),
    page(4, 'scanned_image', 'vlm_full_page', true),
  ];
  assert.deepEqual(methodSentences(pages, 'pdf'), [
    '1·3쪽은 표가 많은 쪽이라 표의 선을 따라 칸을 나눠 읽었어요',
    '2쪽은 글자가 든 PDF라 글자를 그대로 읽었어요',
    '4쪽은 스캔한 쪽이라 쪽 전체를 그림으로 따로 모았어요',
  ]);
});

test('office documents say their tables were read as stored', () => {
  const result = normalizeParseResponse(sampleRaw());
  assert.deepEqual(methodSentences(result.pages, result.document.format), [
    '1쪽은 표가 많은 쪽이라 표를 문서에 든 모양 그대로 읽었어요',
    '2쪽은 글과 그림이 섞인 쪽이라 글자를 그대로 읽고, 그림은 그림·차트로 따로 모았어요',
  ]);
});

test('slides and sheets use their own words and particles', () => {
  assert.deepEqual(
    methodSentences([page(1, 'slide_like', 'native_text'), page(2, 'slide_like', 'native_text')], 'pptx'),
    ['슬라이드 1~2는 글자를 그대로 읽었어요'],
  );
  assert.deepEqual(methodSentences([page(3, 'table_heavy', 'ruled_table')], 'xlsx'), [
    '시트 3은 표가 많은 시트라 표를 문서에 든 모양 그대로 읽었어요',
  ]);
  assert.deepEqual(methodSentences([page(2, 'multi_column', 'multi_column_layout')], 'hwp'), [
    '2쪽은 글이 여러 단으로 나뉜 쪽이라 여러 단을 읽는 순서에 맞춰 읽었어요',
  ]);
});

test('blank and unknown pages still read as a sentence', () => {
  assert.deepEqual(methodSentences([page(5, 'blank', 'native_text')], 'pdf'), ['5쪽은 빈 쪽이라 읽을 내용이 없었어요']);
  assert.deepEqual(methodSentences([page(1, 'new_kind', 'new_strategy')], 'pdf'), ['1쪽은 읽었어요']);
});

test('the 근거 summary counts what was found', () => {
  const result = normalizeParseResponse(sampleRaw());
  assert.equal(foundSummary(result, true), '본문 2쪽 · 표 1개 · 그림·차트 1개 · AI용 문단 2개');
  assert.equal(foundSummary(result, false), '본문 2쪽 · 표 1개 · 그림·차트 1개');
  const partial = { ...result, summary: { totalPages: 10, analyzedPages: 2 } };
  assert.equal(foundSummary(partial, false), '본문 2쪽(전체 10쪽 중) · 표 1개 · 그림·차트 1개');
  const slides = { ...result, document: { ...result.document, format: 'pptx' } };
  assert.equal(foundSummary(slides, false), '슬라이드 2장 · 표 1개 · 그림·차트 1개');
});

test('the file list shows pages and tables', () => {
  const result = normalizeParseResponse(sampleRaw());
  assert.equal(listMeta(result), '2쪽 · 표 1개');
});

test('the ZIP line lists only what the ZIP holds', () => {
  const result = normalizeParseResponse(sampleRaw());
  assert.deepEqual(zipContents(result, { extractCrops: true, includeChunks: true }), [
    '본문',
    '표(CSV)',
    '그림',
    'AI용 문단',
    '처리 기록',
  ]);
  const plain = { ...result, tables: [], visuals: [] };
  assert.deepEqual(zipContents(plain, { extractCrops: true, includeChunks: false }), ['본문', '처리 기록']);
  assert.deepEqual(zipContents(result, { extractCrops: false, includeChunks: false }), ['본문', '표(CSV)', '처리 기록']);
});
