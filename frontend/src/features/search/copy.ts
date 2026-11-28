import { withParticle } from '../../lib/korean.ts';
import type { RerankerKind } from './api.ts';

/**
 * Copy for the "문서 내용 검색" tab.
 *
 * Every sentence uses 해요체; buttons use "무엇을 + 하기". Banned jargon
 * (인덱스, 인덱싱, 쿼리, 질의, 섹션, 업로드, 다운로드) never appears in main UI
 * copy — use 검색 준비, 검색어, 목차 항목, 넣기, 저장하기 instead.
 */

export const startCopy = {
  title: '문서 내용 검색',
  lead: '여러 문서를 넣으면 바로 찾을 수 있게 준비해 주고, 원하는 내용의 원문과 쪽을 보여 줘요.',
  formats: 'PDF · 한글(HWP·HWPX) · 워드(DOC·DOCX) · 파워포인트(PPT·PPTX) · 엑셀(XLSX)',
  limits: '여러 파일 합쳐 최대 50MB까지 넣을 수 있어요',
} as const;

export const settingsCopy = {
  heading: '세부 설정',
  topK: {
    label: '최대 몇 건까지 볼까요?',
    description: '1건부터 20건까지 고를 수 있어요.',
  },
  synonyms: {
    label: '같은 뜻 단어 함께 찾기',
    placeholder: '예: 휴가=연차, 보관=보존',
    description: '쉼표로 묶음을 구분하고, 같은 뜻 단어는 = 또는 |로 이어 적어 주세요.',
  },
  reranker: {
    label: '검색 방식',
    options: [
      { value: 'none' as RerankerKind, label: '빠른 기본 검색 (별도 AI 모델 불필요)' },
      { value: 'onnx' as RerankerKind, label: '정밀 검색 (내 PC의 ONNX 모델 연결)' },
    ],
  },
  rerankerModel: {
    label: '내 PC의 ONNX 모델 파일 경로',
    placeholder: '예: /models/jina_v2_int8/onnx/model_int8.onnx',
    description: '선택한 경우에만 사용하며, 외부로 문서를 보내지 않아요.',
  },
} as const;

export const listCopy = {
  filesHeading: (count: number) => `넣은 문서 (${count})`,
  totalSize: (sizeText: string) => `전체 크기 ${sizeText}`,
  removeFile: (name: string) => `${withParticle(name, '을/를')} 목록에서 빼기`,
  removeButton: '빼기',
  documentsHeading: (count: number) => `찾아볼 범위 (${count})`,
  allDocumentsLabel: (count: number) => `전체 문서 (${count}개)`,
  allDocumentsOption: (docsCount: number, sectionsCount: number) =>
    `전체 문서 · ${docsCount}개, 목차 ${sectionsCount}개`,
  documentMeta: (pagesText: string, sectionsCount: number, tablesCount: number) =>
    tablesCount > 0
      ? `${pagesText} · 목차 ${sectionsCount}개 · 표 ${tablesCount}개`
      : `${pagesText} · 목차 ${sectionsCount}개`,
  documentOption: (name: string, meta: string) => `${name}, ${meta}`,
  unsupportedFormatsHeading: '넣을 수 있는 문서 형식',
} as const;

export const resultCopy = {
  preparingTitle: '문서 검색을 준비하는 중이에요',
  preparingBody: '넣은 문서를 읽고 목차와 표를 정리하고 있어요.',
  errorTitle: '문서 검색 준비를 끝내지 못했어요',
  pickOtherFiles: '다른 파일 넣기',
  retryPrepare: '다시 준비하기',
  dismissNotice: '닫기',
  headerTitle: (docCount: number, sectionCount: number, tableCount: number) =>
    tableCount > 0
      ? `문서 ${docCount}개 · 목차 ${sectionCount}개 · 표 ${tableCount}개`
      : `문서 ${docCount}개 · 목차 ${sectionCount}개`,
  staleNotice: '넣은 문서가 바뀌어서 검색 준비를 다시 해야 해요.',
  partialWarningsTitle: (count: number) => `문서 ${count}개는 읽지 못해 검색 대상에서 빠졌어요.`,
  searchForm: {
    label: '찾을 내용이나 단어',
    placeholder: '예: 보관 기간, 연차 사용 기준',
    submitButton: '내용 찾기',
    emptyQueryError: '찾고 싶은 단어나 문장을 입력해 주세요.',
    searching: '문서에서 내용을 찾는 중이에요…',
  },
  tabs: {
    listLabel: '검색 보기',
    results: (hitCount: number | null) =>
      hitCount !== null ? `찾은 내용 (${hitCount})` : '찾은 내용',
    outline: (sectionCount: number) => `목차 (${sectionCount})`,
    tables: (tableCount: number) => `문서 속 표 (${tableCount})`,
  },
  results: {
    promptTitle: '검색어를 입력해 필요한 내용을 찾아보세요',
    promptBody: '문서마다 목차 항목, 문서 이름, 쪽과 함께 검색어가 굵은 글자와 밑줄로 표시돼요.',
    summary: (count: number, elapsedMs: number, mode: string) => {
      const timeText = elapsedMs >= 1000 ? `${(elapsedMs / 1000).toFixed(1)}초` : `${Math.round(elapsedMs)}ms`;
      const modeLabel = mode === 'fts5+onnx_rerank' ? '정밀 검색' : '빠른 검색';
      return `${modeLabel}으로 ${count}곳을 찾았어요 · ${timeText}`;
    },
    emptyTitle: '일치하는 내용을 찾지 못했어요',
    emptyBody:
      '다른 단어로 다시 찾거나, 세부 설정의 "같은 뜻 단어 함께 찾기"를 추가해 보세요. 목차에서 직접 살펴볼 수도 있어요.',
    browseOutline: '목차에서 직접 보기',
    relevance: (pct: number) => `관련도 ${pct}%`,
    embeddedTables: (count: number) => `포함된 표 ${count}개`,
    lowRelevanceToggle: (count: number) => `관련도가 낮은 결과 ${count}개 더 보기`,
    relatedTablesHeading: '관련 있어 보이는 문서 속 표',
    openTable: '표 보기',
  },
  outline: {
    listHeading: '문서 목차',
    pickHint: '왼쪽 목차 항목을 누르면 잘리지 않은 원문 전체가 여기에 나와요.',
    loading: '목차 항목 원문을 불러오는 중이에요…',
    charCount: (chars: number) => `${chars.toLocaleString('ko-KR')}자`,
    embeddedTablesHeading: '이 목차 항목에 포함된 표',
  },
  tables: {
    emptyTitle: '문서에서 추출한 표가 없어요',
    emptyBody: '글 위주의 문서이거나 표가 그림으로만 들어 있으면 표 목록이 비어 있어요.',
    selectLabel: '볼 표 고르기',
    meta: (pageLabel: string, rows: number, cols: number) =>
      `${pageLabel} · ${rows.toLocaleString('ko-KR')}행 × ${cols}열`,
    columnsLabel: (cols: readonly string[]) => `열: ${cols.join(', ')}`,
    sqlSummary: '조건으로 표 찾아보기 (SQL)',
    sqlHint: '표 이름은 tbl이에요. 합계(SUM), 평균(AVG), 조건(WHERE) 같은 읽기 전용 조회를 실행할 수 있어요.',
    sqlLabel: '조회 조건 (SQL)',
    runSql: '조건으로 찾기',
    runningSql: '표를 조회하는 중이에요…',
    rowCount: (total: number, truncated: boolean) =>
      truncated
        ? `총 ${total.toLocaleString('ko-KR')}행 중 앞부분만 보여 줘요`
        : `총 ${total.toLocaleString('ko-KR')}행`,
  },
} as const;

export const evidenceCopy = {
  heading: '근거',
  idleTitle: '검색 준비가 끝났어요',
  idleBody: '찾은 곳을 고르면 어느 문서 몇 쪽의 어떤 목차 항목인지, 앞뒤 문장과 함께 여기에 보여 줘요.',
  idleFacts: (docs: number, sections: number, tables: number) => [
    `넣은 문서 ${docs}개에서 목차 항목 ${sections}개를 정리했어요.`,
    tables > 0
      ? `문서 속 표 ${tables}개는 [문서 속 표] 탭에서 엑셀용으로 복사할 수 있어요.`
      : '검색 결과는 원문 그대로 앞뒤 문장과 함께 보여 줘요.',
    '모든 찾기와 복사는 내 PC 안에서만 동작해요.',
  ],
  selectedHeading: '찾은 위치와 근거',
  labels: {
    file: '문서 이름',
    location: '위치',
    section: '목차 항목',
    reason: '찾은 이유',
  },
  reasonText: (pct: number, matchedTerms: readonly string[], outlineBoost: number, rerankScore: number | null) => {
    const parts: string[] = [];
    if (matchedTerms.length > 0) {
      parts.push(`일치한 단어: ${matchedTerms.join(', ')}`);
    } else {
      parts.push('제목이나 문맥이 검색어와 비슷해요');
    }
    if (outlineBoost > 0) parts.push('목차 제목에도 겹치는 말이 있어요');
    if (rerankScore !== null) parts.push('정밀 검색 순위를 반영했어요');
    parts.push(`관련도 ${pct}%`);
    return parts.join(' · ');
  },
  passageHeading: '원문 (앞뒤 문장 포함)',
  showFullSection: '목차 항목 전체 보기',
  showPassageOnly: '찾은 부분만 보기',
  copyPassage: '찾은 부분 복사하기',
  copySection: '이 목차 항목 복사하기',
  copiedAnnouncement: (fileName: string, pageLabel: string) =>
    `${fileName} (${pageLabel}) 내용을 복사했어요. 문서나 메신저에 바로 붙여 넣으면 돼요.`,
  copyFailed: '복사하지 못했어요. 아래 원문을 직접 골라 복사해 주세요.',
  detailsSummary: '자세히',
  outlineHeading: '보고 있는 목차 항목',
  tableHeading: '보고 있는 표 정보',
  tableTip: '표 왼쪽 위의 [엑셀용으로 복사하기]를 누르면 엑셀이나 Numbers에 칸이 나뉘어 붙어요.',
} as const;

export const searchErrorCopy = {
  emptyFile: (name: string) => ({
    what: '빈 파일이라 읽을 수 없어요',
    why: `"${name}" 파일의 크기가 0바이트예요.`,
    how: '내용이 들어 있는 다른 문서를 골라 넣어 주세요.',
  }),
  badResponse: {
    what: '검색 결과를 읽지 못했어요',
    why: 'AutoETL이 보낸 응답 형식이 예상과 달랐어요.',
    how: '잠시 뒤 다시 찾아 주세요.',
  },
  sqlFailed: {
    what: '표 조회 조건을 실행하지 못했어요',
    why: '입력한 조건 문법에 맞지 않거나 없는 열 이름을 썼을 수 있어요.',
    how: '열 이름을 확인하고 SELECT 문으로 다시 조회해 주세요.',
  },
  noDocumentsIndexed: {
    what: '넣은 문서를 읽지 못했어요',
    why: '문서 내용이 비어 있거나 읽는 중 오류가 났어요.',
    how: '다른 문서를 넣어 보거나 아래 자세히에서 원인을 확인해 주세요.',
  },
} as const;

export const statusText = {
  idle: '문서를 넣으면 바로 검색을 준비해요',
  preparing: (count: number) => `${count}개 문서의 검색을 준비하는 중이에요`,
  searching: (query: string) => `"${query}" 관련 내용을 찾는 중이에요`,
  loadingSection: '목차 항목 원문을 불러오는 중이에요',
  runningSql: '표 조건 조회를 실행하는 중이에요',
  ready: (docCount: number, sectionCount: number, elapsedSeconds: number) =>
    `문서 ${docCount}개(목차 ${sectionCount}개) 검색 준비 완료 · ${elapsedSeconds.toFixed(1)}초`,
  searched: (count: number, query: string) =>
    count > 0
      ? `"${query}" 검색 완료 · ${count}곳을 찾았어요`
      : `"${query}"와 일치하는 내용을 찾지 못했어요`,
  copied: (fileName: string, pageLabel: string) => `${fileName} (${pageLabel}) 찾은 부분을 복사했어요`,
  stale: '문서 목록이 바뀌었어요 · [다시 준비하기]를 눌러 주세요',
  error: '문제를 확인해 주세요',
} as const;
