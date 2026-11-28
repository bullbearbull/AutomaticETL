import { withParticle } from '../../lib/korean.ts';
import type { DbEngine, MaskingPolicy, OrphanPolicy, Purpose } from './api.ts';

/**
 * Copy for the "엑셀·CSV로 DB 만들기" tab.
 *
 * Every sentence uses 해요체; buttons use "무엇을 + 하기". Internal terms
 * (컬럼, PK, FK, DDL, SQL, 마스킹, 해시, 적재, 파싱) stay out of the main
 * screen and appear only inside folded "자세히" sections when needed.
 */

export const startCopy = {
  title: '엑셀·CSV로 DB 만들기',
  lead: '표 파일을 넣으면 열 성격과 표 연결을 살펴보고, 바로 DB 파일로 만들어 줘요.',
  formats: 'CSV(.csv) · TSV(.tsv) · 엑셀(.xlsx)',
  limits: '여러 파일 합쳐 최대 50MB까지 넣을 수 있어요',
} as const;

export const purposeCopy = {
  heading: '저장 목적',
  options: [
    {
      value: 'oltp' as Purpose,
      label: '일반 서비스용',
      hint: '중복을 줄이고 표끼리 연결해 깔끔하게 저장해요',
    },
    {
      value: 'analytics' as Purpose,
      label: '통계·분석용',
      hint: '집계와 조회가 빠르도록 표를 정리해 저장해요',
    },
    {
      value: 'archive' as Purpose,
      label: '원본 보관용',
      hint: '원래 표 모양을 최대한 그대로 남겨서 저장해요',
    },
  ],
  archiveNotice:
    '원본 보관용은 저장 직전에 개인정보를 다시 묻지 않아요. 가릴 열은 표 구조의 "저장할 때"에서 미리 골라 주세요.',
} as const;

export const maskingCopy: Record<MaskingPolicy, { label: string; hint: string }> = {
  none: { label: '그대로', hint: '원래 값 그대로 저장해요' },
  mask: { label: '가리기', hint: '일부를 *로 가려 저장해요' },
  hash: { label: '알아볼 수 없게 바꾸기', hint: '같은 값만 구분할 수 있는 코드로 바꿔 저장해요' },
};

export const settingsCopy = {
  heading: '세부 설정',
  dbEngine: {
    label: 'DB 파일 종류',
    options: [
      { value: 'sqlite' as DbEngine, label: 'SQLite (.sqlite) — 가볍고 어디서나 열기 좋아요' },
      { value: 'duckdb' as DbEngine, label: 'DuckDB (.duckdb) — 큰 표의 집계·분석이 빨라요' },
    ],
  },
  decomposeStrings: {
    label: '코드 속 숨은 정보 분리하기',
    description: '코드나 번호 문자열 안에 의미가 나뉘어 있으면 자동으로 풀어내 분석에 써요.',
  },
  orphanPolicy: {
    label: '오류 행 처리',
    options: [
      { value: 'quarantine' as OrphanPolicy, label: '따로 모아 저장하기 (권장) — 문제 있는 행만 따로 빼 두고 나머지를 저장해요' },
      { value: 'reject_all' as OrphanPolicy, label: '오류가 있으면 저장 멈추기 — 문제 있는 행이 나오면 저장을 취소해요' },
    ],
  },
  includeAuditColumns: {
    label: '언제·어디서 넣었는지 이력 열 함께 넣기',
    description: '각 표에 저장 묶음·원본 파일·저장 시각 열을 함께 남겨요.',
  },
  includeManagementTables: {
    label: 'DB 안에 관리 표 함께 만들기',
    description: '저장 기록과 따로 모은 오류 행을 찾아볼 수 있는 관리 표를 함께 만들어요.',
  },
  notes: {
    label: '저장 메모',
    placeholder: '예: 2026년 1분기 고객·주문 정리본',
    description: 'DB 파일 안의 저장 기록에 함께 남기는 짧은 메모예요.',
  },
} as const;

export const listCopy = {
  filesHeading: (count: number) => `넣은 파일 (${count})`,
  totalSize: (sizeText: string) => `전체 크기 ${sizeText}`,
  removeFile: (name: string) => `${withParticle(name, '을/를')} 목록에서 빼기`,
  removeButton: '빼기',
  tablesHeading: (count: number) => `표 (${count})`,
  tableOption: (name: string, meta: string) => `${name}, ${meta}`,
  tableMeta: (rows: number, cols: number) => `행 ${rows.toLocaleString('ko-KR')}개 · 열 ${cols}개`,
  tableUnmaskedPii: (count: number) => `개인정보 ${count}개 미보호`,
  tableAllProtected: (count: number) => `개인정보 ${count}개 보호됨`,
  unsupportedFormatsHeading: '넣을 수 있는 파일 형식',
} as const;

export const resultCopy = {
  loadingTitle: '표를 살펴보는 중이에요',
  loadingBody: '열의 성격과 표끼리의 연결을 자동으로 찾고 있어요.',
  errorTitle: '표를 살펴보지 못했어요',
  pickOtherFiles: '다른 파일 넣기',
  retryAnalyze: '다시 살펴보기',
  dismissNotice: '닫기',
  tabs: {
    listLabel: '결과 보기',
    schema: '표 구조',
    relations: (fkCount: number) => (fkCount > 0 ? `표 연결 (${fkCount})` : '표 연결'),
    plan: '저장 순서·정리',
    saved: '저장 결과',
  },
  surrogateSourceLabel: '자동으로 붙인 번호',
  decomposedSourceLabel: (sourceCol: string) => `${sourceCol}에서 나눈 열`,
  readOnlySurrogateNote: '자동 생성',
  readOnlyDecomposedNote: '나눈 열',
  tableSummary: (sourceKindLabel: string, rows: number, cols: number, sheet: string | null) => {
    const parts = [sourceKindLabel];
    if (sheet) parts.push(`시트 "${sheet}"`);
    parts.push(`행 ${rows.toLocaleString('ko-KR')}개`, `열 ${cols}개`);
    return parts.join(' · ');
  },
  resetOverrides: '원래대로 되돌리기',
  schemaScrollRegion: (tableName: string) => `${tableName} 표 구조, 옆으로 밀어 볼 수 있어요`,
  schemaColumns: {
    sourceColumn: '원래 열',
    targetColumn: 'DB 열 이름',
    valueKind: '값 종류',
    unique: '중복 금지',
    masking: '저장할 때',
  },
  badges: {
    primaryKey: '대표 키',
    piiUnmasked: '개인정보',
    piiProtected: '개인정보 보호됨',
    recommendedUnique: '중복 없음',
  },
  targetColumnInputLabel: (colName: string) => `${colName}의 DB 열 이름`,
  uniqueInputLabel: (colName: string) => `${colName} 열 중복 금지`,
  maskingInputLabel: (colName: string) => `${colName} 열 저장할 때 가리기 방식`,
  missingRate: (count: number, rate: number) =>
    count === 0 ? '빈 칸 없음' : `빈 칸 ${count.toLocaleString('ko-KR')}개 (${Math.round(rate * 100)}%)`,
  revalidatingNotice: '고친 내용에 맞춰 다시 확인하는 중이에요…',
  relations: {
    pkHeading: '각 표의 대표 키(행을 구분하는 열)',
    pkRow: (tableName: string, cols: string[], surrogate: string | null) =>
      surrogate
        ? `${tableName}: 자동 일련번호(${surrogate})를 새로 만들어요 (후보: ${cols.join(', ')})`
        : `${tableName}: ${cols.join(', ')}`,
    fkHeading: '표끼리 연결된 관계',
    fkNone: '표 사이에 연결된 관계가 없어요. 각 표를 따로 저장해요.',
    fkCard: (child: string, childCols: string[], parent: string, parentCols: string[], cardinality: string) =>
      `${child} (${childCols.join(', ')}) → ${parent} (${parentCols.join(', ')}) · ${cardinalityLabel(cardinality)}`,
    fkMatch: (rate: number, orphans: number) => {
      const pct = Math.round(rate * 1000) / 10;
      return orphans > 0
        ? `일치율 ${pct}% · 연결되지 않는 행 ${orphans.toLocaleString('ko-KR')}개`
        : `일치율 ${pct}% · 연결되지 않는 행 없음`;
    },
  },
  plan: {
    orderHeading: '저장하는 순서',
    orderItem: (index: number, table: string, rows: number, dependsOn: string[]) => {
      const base = `${index + 1}. ${table} (예상 ${rows.toLocaleString('ko-KR')}행)`;
      return dependsOn.length > 0 ? `${base} — 먼저 저장할 표: ${dependsOn.join(', ')}` : base;
    },
    cycleWarning: '서로 순환 참조가 있어 저장 순서를 조정해야 할 수 있어요:',
    detailsSummary: '자세히 (DB 생성 코드·설계 보고서)',
    copyDdl: 'DB 생성 코드 복사하기',
    copiedDdl: 'DB 생성 코드를 복사했어요.',
    copyDdlFailed: 'DB 생성 코드를 복사하지 못했어요. 직접 골라 복사해 주세요.',
    ddlHeading: 'DB 생성 코드',
    managementDdlHeading: '관리 표 생성 코드',
    erdHeading: '표 관계 그림 코드',
    scorecardHeading: '구성 방식 비교',
    scorecardColumns: ['방식', '종합 점수', '무결성', '중복 감소', '모의 저장'] as const,
    trialPass: '통과',
    trialWarn: '주의 필요',
    reportHeading: '전체 분석 보고서',
  },
  saved: {
    heading: 'DB 저장을 마쳤어요',
    summary: (loaded: number, read: number, quarantined: number) =>
      quarantined > 0
        ? `전체 ${read.toLocaleString('ko-KR')}행 중 ${loaded.toLocaleString('ko-KR')}행을 저장하고, ${quarantined.toLocaleString('ko-KR')}행을 따로 모아 뒀어요.`
        : `전체 ${read.toLocaleString('ko-KR')}행을 모두 저장했어요.`,
    skippedDuplicate: '방금 전과 같은 파일과 설정이라 이미 저장된 결과를 그대로 사용해요.',
    partialNote: '일부 오류 행은 따로 모아 두고 나머지를 모두 저장했어요.',
    failedTitle: 'DB 파일을 만들지 못했어요',
    failedBody: '저장 설정이나 오류 행을 확인한 뒤 다시 시도해 주세요.',
    warningsHeading: '저장 중 확인된 내용',
    redownload: 'DB 파일 다시 저장하기',
    meta: (engineLabel: string, localTime: string, filename: string) =>
      `${engineLabel} · ${localTime} · 파일 이름: ${filename}`,
    tableHeading: '표별 저장 내역',
    tableColumns: ['표 이름', '원본 파일', '읽은 행', '저장한 행', '따로 모은 행', '보호한 열'] as const,
    noMaskedColumns: '없음',
    quarantineHeading: (count: number) => `따로 모은 오류 행 (${count}건)`,
    quarantineColumns: ['표 이름', '원본 줄', '열 이름', '원인', '내용'] as const,
  },
} as const;

export const validationCopy = {
  invalidIdentifier: '소문자 영어(a-z)로 시작해 소문자·숫자·밑줄(_)만 쓸 수 있어요.',
  fixIdentifiersFirst: 'DB 열 이름에 고칠 곳이 있어요. 빨간색으로 표시된 열 이름을 먼저 확인해 주세요.',
} as const;

export const piiDialogCopy = {
  title: (count: number) => `개인정보로 보이는 열 ${count}개가 아직 가려지지 않았어요`,
  lead: (count: number) =>
    count === 1
      ? '아래 열은 "그대로"로 되어 있어 원래 값이 DB 파일에 저장돼요.'
      : '아래 열들은 "그대로"로 되어 있어 원래 값이 DB 파일에 저장돼요.',
  tableLine: (tableName: string, columns: readonly string[]) =>
    `${withParticle(tableName, '은/는')} ${columns.join(', ')} 열이에요.`,
  previewLine: (examples: string) => `가리면 ${examples}처럼 바뀌어 저장돼요.`,
  noteArchiveTip: '열별 가리기 방식은 표 구조의 "저장할 때"에서 언제든 따로 정할 수 있어요.',
  primaryAction: '가리고 저장하기',
  secondaryAction: '그대로 저장하기',
} as const;

export const evidenceCopy = {
  piiHeading: '개인정보 확인',
  piiNoneInTable: '이 표에서는 개인정보로 보이는 열을 찾지 못했어요.',
  piiInOtherTables: (tables: readonly string[]) =>
    `다른 표(${tables.join(', ')})에 가려지지 않은 개인정보 열이 있어요.`,
  piiReasonLabel: '찾은 이유',
  piiPreviewLabel: '가렸을 때의 모습 (예시)',
  piiReasonText: (
    columnName: string,
    typeLabels: string[],
    level: string | null,
    maskPattern: string | null,
    recommendation?: string,
  ) => {
    const levelText = level ? ` · 등급: ${levelLabel(level)}` : '';
    const patternText = maskPattern ? ` · 값 모양: ${maskPattern}` : '';
    const base = `${columnName} 열 — 유형: ${typeLabels.join(', ')}${levelText}${patternText}`;
    return recommendation ? `${base} (${recommendation})` : base;
  },
  piiPreviewValue: (policy: MaskingPolicy, maskExample: string, hashExample: string) => {
    if (policy === 'mask') return `가리기 적용 중 — ${maskExample}`;
    if (policy === 'hash') return `알아볼 수 없게 바꾸기 적용 중 — ${hashExample}`;
    return `가리면 ${maskExample} / 바꾸면 ${hashExample}`;
  },
  pkHeading: '대표 키와 검증 근거',
  pkNone: '대표 키로 자동 지정된 열이 없어요.',
  validationPassed: '모의 저장 검사에서 문제가 없었어요.',
  validationIssues: (constraints: number, casts: number) =>
    `모의 저장 검사에서 제약 오류 ${constraints}건, 형식 변환 오류 ${casts}건이 보였어요.`,
  warningsHeading: '함께 참고할 점',
  formatsHeading: '넣을 수 있는 파일',
  formatsNote: 'CSV(.csv), TSV(.tsv), 엑셀(.xlsx) 파일을 넣을 수 있어요. 구형 엑셀(.xls)은 엑셀에서 .xlsx로 다시 저장한 뒤 넣어 주세요.',
} as const;

export const exportCopy = {
  summary: (engine: DbEngine, tableCount: number, unmaskedCount: number) => {
    const engineLabel = engine === 'duckdb' ? 'DuckDB (.duckdb)' : 'SQLite (.sqlite)';
    if (unmaskedCount > 0) {
      return `${engineLabel} · 표 ${tableCount}개 · 가려지지 않은 개인정보 ${unmaskedCount}개`;
    }
    return `${engineLabel} · 표 ${tableCount}개 · 모든 열 확인됨`;
  },
  saveZip: '설계안 ZIP으로 저장하기',
  saveDb: 'DB 파일로 저장하기',
  reanalyze: '표 다시 살펴보기',
  staleNotice: '파일 목록이 바뀌어 다시 분석이 필요해요. 분석이 끝난 뒤 저장할 수 있어요.',
  savingDb: 'DB 파일을 만들고 있어요…',
  savedDb: (filename: string) => `${withParticle(filename, '을/를')} 저장하기 시작했어요.`,
  savedZip: (filename: string) => `${withParticle(filename, '을/를')} 저장하기 시작했어요.`,
  zipMissing: '설계안 ZIP이 서버에 남아 있지 않아요. 표를 고치거나 다시 열어 주세요.',
  zipFailed: '설계안 ZIP을 저장하지 못했어요. 잠시 뒤 다시 시도해 주세요.',
  dbDownloadMissing: 'DB 파일이 서버에 남아 있지 않아요. [DB 파일로 저장하기]를 다시 눌러 주세요.',
  dbDownloadFailed: 'DB 파일을 내려받지 못했어요. 잠시 뒤 다시 시도해 주세요.',
} as const;

export const statusText = {
  idle: 'CSV, TSV, 엑셀(.xlsx) 파일을 넣으면 자동으로 살펴봐요',
  analyzing: (count: number) =>
    count === 1 ? '파일 1개를 분석하고 표 구조를 찾는 중이에요' : `파일 ${count}개를 분석하고 표 관계를 찾는 중이에요`,
  revalidating: '고친 설정에 맞춰 표 구조를 다시 확인하고 있어요',
  savingDb: (engine: DbEngine) =>
    engine === 'duckdb' ? 'DuckDB(.duckdb) 파일을 만들고 있어요' : 'SQLite(.sqlite) 파일을 만들고 있어요',
  ready: (tableCount: number, fkCount: number, elapsedSeconds: number) => {
    const timeText = elapsedSeconds > 0 ? ` (${elapsedSeconds.toFixed(1)}초)` : '';
    return fkCount > 0
      ? `표 ${tableCount}개와 연결 관계 ${fkCount}개를 찾았어요${timeText}`
      : `표 ${tableCount}개를 확인했어요${timeText}`;
  },
  readyUnmaskedWarning: (tableCount: number, unmaskedCount: number) =>
    `표 ${tableCount}개를 확인했어요 · 가려지지 않은 개인정보 열이 ${unmaskedCount}개 있어요`,
  stale: '파일 목록이 바뀌었어요. 다시 살펴본 뒤 저장해 주세요',
  saved: (filename: string, loadedRows: number) =>
    `${withParticle(filename, '을/를')} 만들었어요 (총 ${loadedRows.toLocaleString('ko-KR')}행 저장됨)`,
  error: '작업을 마치지 못했어요. 화면의 안내를 확인해 주세요',
} as const;

export const databaseErrorCopy = {
  emptyFile: (name: string) => ({
    what: `${withParticle(name, '이/가')} 비어 있어요`,
    why: '크기가 0바이트인 빈 파일에서는 표를 읽을 수 없어요.',
    how: '내용이 들어 있는 파일로 다시 넣어 주세요.',
  }),
  noTablesFound: {
    what: '읽을 수 있는 표를 찾지 못했어요',
    why: '파일에 표 머리글과 행이 들어 있지 않을 수 있어요.',
    how: '첫 줄에 열 이름이 있는 CSV·TSV·엑셀(.xlsx) 파일인지 확인해 주세요.',
  },
  loadFailed: {
    what: 'DB 파일을 만들다 멈췄어요',
    why: '오류 행이 있거나, 표 사이의 연결 조건에 맞지 않는 값이 들어 있을 수 있어요.',
    how: '오류 행 처리 방식을 확인하거나, 표 구조를 다시 살펴본 뒤 저장해 주세요.',
  },
} as const;

export function cardinalityLabel(raw: string): string {
  switch (raw) {
    case '1:1':
      return '일대일(1:1)';
    case '1:N':
      return '일대다(1:N)';
    case 'N:1':
      return '다대일(N:1)';
    case 'N:M':
      return '다대다(N:M)';
    default:
      return raw || '연결';
  }
}

export function levelLabel(level: string): string {
  switch (level) {
    case 'L3':
      return '고위험 식별정보';
    case 'L2':
      return '개인정보';
    case 'L1':
      return '민감 참고정보';
    default:
      return level;
  }
}

export function physicalTypeLabel(physical: string, semantic: string): string {
  if (semantic === 'identifier' || semantic === 'code_identifier') return '식별 코드';
  if (semantic === 'email') return '이메일';
  if (semantic === 'phone') return '전화번호';
  if (semantic === 'temporal') return '날짜·시간';
  if (semantic === 'continuous_numeric') return '숫자·금액';
  if (semantic === 'discrete_numeric' || physical === 'integer') return '정수';
  if (physical === 'decimal' || physical === 'float') return '소수';
  if (physical === 'boolean') return '참/거짓';
  if (physical === 'date') return '날짜';
  if (physical === 'timestamp' || physical === 'timestamptz') return '날짜·시간';
  return '문자';
}
