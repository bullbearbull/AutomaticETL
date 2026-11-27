/**
 * A made-up POST /api/parse response for the extract tab tests: a two-page
 * 워드 document with one table and one figure. Every value is invented.
 */

export const TINY_PNG =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';

export const SAMPLE_TABLE_MARKDOWN = [
  '*표 1. 문서 종류별 담당 부서*',
  '',
  '| 구분 | 담당 부서 | 비고 |',
  '| --- | --- | --- |',
  '| 회의록 | 총무팀 | 전자 문서로 보관 |',
  '| 계약서 | 법무팀 | 원본은 금고에 보관 |',
  '| 출장 보고서 | 각 부서 | 결재 후 등록 |',
].join('\n');

export const SAMPLE_MARKDOWN = [
  '# 문서관리 규정 (예시)',
  '',
  '> 출처: `문서관리_규정_예시.docx` · 형식: `docx` · 분석 페이지: 2/2 · 추출 표: 1개 · 시각 영역: 1개',
  '',
  '<!-- Page 1 | category=table_heavy | strategy=ruled_table -->',
  '## Page 1 (표 중심 · 격자선 표 복원)',
  '',
  '### 제1조(목적)',
  '',
  '이 규정은 예시 회사가 문서를 만들고 보관하는 방법을 정해요.',
  '',
  '### 제3조(문서 종류)',
  '',
  '문서 종류마다 담당 부서와 보관 방법은 아래 표와 같아요.',
  '',
  SAMPLE_TABLE_MARKDOWN,
  '',
  '---',
  '',
  '<!-- Page 2 | category=hybrid_text_image | strategy=native_text -->',
  '## Page 2 (텍스트+시각 혼재 · 네이티브 텍스트 추출)',
  '',
  '### 제4조(보관 흐름)',
  '',
  '문서는 작성, 검토, 보관 순서로 처리해요.',
  '',
  '> **[FIGURE_IMAGE · p002_v01]** 권장 전략: `vlm_region` (부분 영역 VLM 해석)',
  '> - 판정 근거: DOCX 삽입 이미지 `Picture 1` (240x120px, 면적 4.7%) — OCR/VLM 시각 해석 대상',
  '> - 위치(pt): `(72.0, 129.45) ~ (288.0, 237.45)`',
  '> - 상태: `pending_vision` / 크롭: `saved`',
  '> - 캡션: 그림 1. 문서 보관 흐름 (예시)',
  '> - 크롭 파일: [p002_v01.png](/tmp/example_req/bundle/crops/p002_v01.png)',
  '',
  '### 제5조(보관 기간)',
  '',
  '보관 기간은 문서 종류마다 달라요.',
  '',
  '---',
  '',
].join('\n');

/** The raw JSON, shaped like the server's answer. */
export function sampleRaw(): Record<string, unknown> {
  return {
    bundle_id: '6dec1208d4cdf8e2bfadfc3c',
    elapsed_seconds: 0.008,
    document: {
      source_path: '문서관리_규정_예시.docx',
      format: 'docx',
      file_size_bytes: 3284,
      page_count: 2,
      title: '문서관리 규정 (예시)',
    },
    routing_plan: {
      summary: { total_pages: 2, analyzed_pages: 2, table_count: 1, visual_region_count: 1 },
      pages: [
        { page: 1, category: 'table_heavy', primary_strategy: 'ruled_table', requires_vision: false },
        { page: 2, category: 'hybrid_text_image', primary_strategy: 'native_text', requires_vision: true },
      ],
    },
    markdown: SAMPLE_MARKDOWN,
    routing_report_markdown: '# 문서 구조 진단 및 파싱 전략 리포트 — `문서관리_규정_예시.docx`\n',
    tables: [
      {
        table_id: 'p001_t01',
        page: 1,
        caption: '표 1. 문서 종류별 담당 부서',
        strategy: 'ruled_table',
        row_count: 4,
        col_count: 3,
        headers: ['구분', '담당 부서', '비고'],
        rows: [
          ['회의록', '총무팀', '전자 문서로 보관'],
          ['계약서', '법무팀', '원본은 금고에 보관'],
          ['출장 보고서', '각 부서', '결재 후 등록'],
        ],
        csv: '구분,담당 부서,비고\r\n',
        markdown: SAMPLE_TABLE_MARKDOWN,
      },
    ],
    chunks: [
      {
        chunk_id: '3777d473_c0000',
        page_start: 1,
        page_end: 1,
        char_count: 44,
        text: '[제1조(목적)]\n이 규정은 예시 회사가 문서를 만들고 보관하는 방법을 정해요.',
      },
      {
        chunk_id: '3777d473_c0001',
        page_start: 1,
        page_end: 2,
        char_count: 30,
        text: '[제4조(보관 흐름)]\n문서는 작성, 검토, 보관 순서로 처리해요.',
      },
    ],
    visuals: [
      {
        region_id: 'p002_v01',
        page: 2,
        element_kind: 'figure_image',
        recommended_strategy: 'vlm_region',
        reason: 'DOCX 삽입 이미지 `Picture 1` (240x120px, 면적 4.7%) — OCR/VLM 시각 해석 대상',
        caption: '그림 1. 문서 보관 흐름 (예시)',
        crop_status: 'saved',
        image_data_url: TINY_PNG,
      },
    ],
    warnings: [],
  };
}
