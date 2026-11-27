import assert from 'node:assert/strict';
import { test } from 'node:test';
import { normalizeParseResponse } from '../../src/features/extract/api.ts';
import { buildBody, shiftHeading, type BodySegment } from '../../src/features/extract/body.ts';
import { SAMPLE_TABLE_MARKDOWN, sampleRaw } from './extract-sample.ts';

function sampleBody() {
  const result = normalizeParseResponse(sampleRaw());
  return buildBody({ markdown: result.markdown, tables: result.tables, visuals: result.visuals, unit: 'page' });
}

function texts(segments: readonly BodySegment[]): string[] {
  return segments.flatMap((segment) => (segment.kind === 'text' ? [segment.markdown] : []));
}

test('the body keeps the document order: text, table, page mark, text, figure, text', () => {
  const { segments } = sampleBody();
  assert.deepEqual(
    segments.map((segment) => segment.kind),
    ['text', 'table', 'page', 'text', 'visual', 'text'],
  );
  assert.deepEqual(segments[1], { kind: 'table', index: 0, caption: '표 1. 문서 종류별 담당 부서', continued: false });
  assert.deepEqual(segments[2], { kind: 'page', page: 2 });
  assert.deepEqual(segments[4], { kind: 'visual', index: 0 });
});

test('processing notes are taken out of the text', () => {
  const all = texts(sampleBody().segments).join('\n');
  for (const note of ['<!--', 'category=', 'strategy=', 'Page 1', 'Page 2', '출처', 'FIGURE_IMAGE', '크롭', '---']) {
    assert.ok(!all.includes(note), `text still holds ${note}`);
  }
});

test('headings move under the file name heading', () => {
  const [first] = texts(sampleBody().segments);
  assert.ok(first?.startsWith('### 문서관리 규정 (예시)\n\n#### 제1조(목적)'), first);
  assert.equal(shiftHeading('# 제목'), '### 제목');
  assert.equal(shiftHeading('## 둘'), '### 둘');
  assert.equal(shiftHeading('### 셋'), '#### 셋');
  assert.equal(shiftHeading('###### 여섯'), '###### 여섯');
  assert.equal(shiftHeading('#해시태그'), '#해시태그');
});

test('the copy text is clean markdown with the table and a figure line', () => {
  const { copyText } = sampleBody();
  assert.ok(copyText.startsWith('# 문서관리 규정 (예시)\n\n### 제1조(목적)'), copyText);
  assert.ok(copyText.includes(SAMPLE_TABLE_MARKDOWN));
  assert.ok(copyText.includes('[그림: 그림 1. 문서 보관 흐름 (예시)]'));
  assert.ok(!copyText.includes('<!--'));
  assert.ok(!copyText.includes('출처'));
  assert.ok(!/\n{3,}/.test(copyText));
});

test('a table is matched only as a block of its own', () => {
  const table = '| 가 | 나 |\n| --- | --- |\n| 1 | 2 |';
  const markdown = ['<!-- Page 1 | category=digital_text | strategy=native_text -->', '## Page 1 (디지털 본문 · 네이티브 텍스트 추출)', '', table, '', '끝'].join('\n');
  const body = buildBody({ markdown, tables: [{ markdown: table, caption: null }], visuals: [], unit: 'page' });
  assert.deepEqual(body.segments.map((segment) => segment.kind), ['table', 'text']);

  const glued = ['문단 바로 뒤', table].join('\n');
  const unmatched = buildBody({ markdown: glued, tables: [{ markdown: table, caption: null }], visuals: [], unit: 'page' });
  assert.deepEqual(unmatched.segments.map((segment) => segment.kind), ['text']);
});

test('a continued table is marked as continued', () => {
  const table = '*(이전 페이지 표 `p001_t01`에서 이어짐)*\n\n| 가 |\n| --- |\n| 1 |';
  const body = buildBody({ markdown: `${table}\n`, tables: [{ markdown: table, caption: null }], visuals: [], unit: 'page' });
  assert.deepEqual(body.segments, [{ kind: 'table', index: 0, caption: null, continued: true }]);
});

test('an empty page gets a plain note in the unit of the file', () => {
  const markdown = [
    '<!-- Page 1 | category=blank | strategy=native_text -->',
    '## Page 1 (빈 페이지 · 네이티브 텍스트 추출)',
    '',
    '*(추출된 본문 블록이 없는 페이지)*',
    '',
  ].join('\n');
  const body = buildBody({ markdown, tables: [], visuals: [], unit: 'slide' });
  assert.deepEqual(body.segments, [{ kind: 'text', markdown: '*이 슬라이드에서는 읽을 글을 찾지 못했어요.*' }]);
  assert.equal(body.copyText, '');
});

test('fenced code is left exactly as it is', () => {
  const markdown = ['```text', '<!-- Page 9 | category=x | strategy=y -->', '# 그대로', '---', '```', ''].join('\n');
  const body = buildBody({ markdown, tables: [], visuals: [], unit: 'page' });
  assert.deepEqual(body.segments, [{ kind: 'text', markdown: markdown.trimEnd() }]);
});

test('a figure the vision step described becomes a figure part', () => {
  const markdown = ['<!-- p001_v01: vector_diagram via local -->', '*흐름도*', '<sub>시각 해석: `local`</sub>', '', '설명 글'].join('\n');
  const body = buildBody({
    markdown,
    tables: [],
    visuals: [{ id: 'p001_v01', kind: 'vector_diagram', caption: '흐름도' }],
    unit: 'page',
  });
  assert.deepEqual(body.segments.map((segment) => segment.kind), ['visual', 'text']);
  assert.ok(body.copyText.startsWith('[도식·차트: 흐름도]'));
  assert.ok(!body.copyText.includes('<sub>'));
});

test('a title that only repeats the file name is left out of the view', () => {
  const markdown = '# 보고서.pdf\n\n본문';
  const body = buildBody({ markdown, tables: [], visuals: [], unit: 'page', fileName: '보고서.pdf' });
  assert.deepEqual(body.segments, [{ kind: 'text', markdown: '본문' }]);
  assert.equal(body.copyText, '# 보고서.pdf\n\n본문');
});

test('a table with very many rows does not overflow the call stack', () => {
  // Spreading this many lines into one call throws RangeError in V8.
  const rows = Array.from({ length: 150_000 }, (_, index) => `| ${index} | 값 |`);
  const table = ['| 번호 | 값 |', '| --- | --- |', ...rows].join('\n');
  const markdown = ['<!-- Page 1 | category=digital_text | strategy=native_text -->', '## Page 1 (디지털 본문 · 네이티브 텍스트 추출)', '', table, '', '끝'].join('\n');
  const body = buildBody({ markdown, tables: [{ markdown: table, caption: null }], visuals: [], unit: 'page' });
  assert.deepEqual(body.segments.map((segment) => segment.kind), ['table', 'text']);
  assert.ok(body.copyText.includes(table));
});
