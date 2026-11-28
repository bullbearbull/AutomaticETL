/**
 * Text that only the extract tab uses. Sentences use 해요체; button, tab and
 * column names are short nouns or "무엇을 + 하기". Text that several tabs
 * share stays in copy/common.ts.
 */
import { statusCopy } from '../../copy/common.ts';
import { endsWithFinalConsonant, withParticle } from '../../lib/korean.ts';

/** A file name or typed value in curly quotes, as the status line shows it. */
export function quoted(text: string): string {
  return `“${text}”`;
}

/** "표가 많은 쪽" → "표가 많은 쪽이라", "글자가 든 PDF" → "글자가 든 PDF라". */
export function withCopula(phrase: string): string {
  return phrase + (endsWithFinalConsonant(phrase) ? '이라' : '라');
}

export const startCopy = {
  title: 'PDF·한글·오피스 문서에서 글과 표를 뽑아요',
  lead: '파일을 넣으면 본문·표·그림을 정리해요. 파일은 내 PC 밖으로 나가지 않아요.',
  formats: 'PDF · 한글(HWP·HWPX) · 워드 · 파워포인트 · 엑셀(XLSX)',
  limits: '파일 하나에 50MB까지 · 여러 개를 한 번에 넣어도 돼요',
} as const;

export const statusText = {
  ready: `${statusCopy.ready} · 파일을 넣으면 바로 읽기 시작해요`,
  reading: (name: string, position: { index: number; total: number } | null) =>
    `${withParticle(quoted(name), '을/를')} 읽는 중이에요` +
    (position ? ` · 파일 ${position.total}개 중 ${position.index}번째` : ''),
  doneOne: (name: string, tables: number, visuals: number) =>
    `${withParticle(quoted(name), '을/를')} 다 읽었어요 · 표 ${tables}개 · 그림·차트 ${visuals}개`,
  failedOne: (name: string) => `${withParticle(quoted(name), '을/를')} 읽지 못했어요`,
  doneAll: (total: number) => `파일 ${total}개를 다 읽었어요`,
  doneSome: (total: number, done: number, failed: number) =>
    `파일 ${total}개 중 ${done}개를 다 읽었어요 · ${failed}개는 읽지 못했어요`,
  failedAll: (total: number) => `파일 ${total}개를 모두 읽지 못했어요`,
} as const;

export const formatCopy = {
  names: {
    pdf: 'PDF 문서',
    hwp: '한글 문서',
    hwpx: '한글 문서',
    doc: '워드 문서',
    docx: '워드 문서',
    ppt: '파워포인트 문서',
    pptx: '파워포인트 문서',
    xlsx: '엑셀 문서',
  } as Readonly<Record<string, string>>,
  other: (extensionWithoutDot: string) =>
    extensionWithoutDot ? `${extensionWithoutDot.toUpperCase()} 파일` : '확장자가 없는 파일',
  /** For error messages: "읽을 수 있는 형식은 …예요". */
  supportedList: 'PDF·한글(HWP·HWPX)·워드(DOC·DOCX)·파워포인트(PPT·PPTX)·엑셀(XLSX)',
  /** For the 근거 column next to a file error. */
  supportedItems: ['PDF', '한글 (HWP·HWPX)', '워드 (DOC·DOCX)', '파워포인트 (PPT·PPTX)', '엑셀 (XLSX)'],
  sizeLimit: '파일 하나에 50MB까지',
} as const;

/**
 * How pages are named. PDF, 한글 and 워드 count 쪽; 파워포인트 counts
 * slides; 엑셀 counts sheets.
 */
export const unitCopy = {
  page: {
    noun: '쪽',
    where: (n: number) => `${n}쪽`,
    range: (text: string) => `${text}쪽`,
    many: (text: string, count: number) => `${text} 등 ${count}쪽`,
    count: (n: number) => `${n}쪽`,
    body: (n: number) => `본문 ${n}쪽`,
    ofTotal: (n: number) => `전체 ${n}쪽 중`,
    noText: '이 쪽에서는 읽을 글을 찾지 못했어요.',
  },
  slide: {
    noun: '슬라이드',
    where: (n: number) => `슬라이드 ${n}`,
    range: (text: string) => `슬라이드 ${text}`,
    many: (text: string, count: number) => `슬라이드 ${text} 등 ${count}장`,
    count: (n: number) => `슬라이드 ${n}장`,
    body: (n: number) => `슬라이드 ${n}장`,
    ofTotal: (n: number) => `전체 ${n}장 중`,
    noText: '이 슬라이드에서는 읽을 글을 찾지 못했어요.',
  },
  sheet: {
    noun: '시트',
    where: (n: number) => `시트 ${n}`,
    range: (text: string) => `시트 ${text}`,
    many: (text: string, count: number) => `시트 ${text} 등 ${count}개`,
    count: (n: number) => `시트 ${n}개`,
    body: (n: number) => `시트 ${n}개`,
    ofTotal: (n: number) => `전체 ${n}개 중`,
    noText: '이 시트에서는 읽을 글을 찾지 못했어요.',
  },
} as const;

/** Plain words for the routing plan: why a page was read the way it was. */
export const routingCopy = {
  why: {
    digitalTextPdf: '글자가 든 PDF',
    multiColumn: (noun: string) => `글이 여러 단으로 나뉜 ${noun}`,
    tableHeavy: (noun: string) => `표가 많은 ${noun}`,
    scannedPdf: '스캔한 쪽',
    imageOnly: (noun: string) => `그림으로만 된 ${noun}`,
    hybrid: (noun: string) => `글과 그림이 섞인 ${noun}`,
    slideLike: '슬라이드처럼 생긴 쪽',
    diagram: (noun: string) => `도식·차트가 많은 ${noun}`,
    blank: (noun: string) => `빈 ${noun}`,
  },
  /** [sentence end, joining form] — the joining form continues with the visual tail. */
  how: {
    nativeText: ['글자를 그대로 읽었어요', '글자를 그대로 읽고'],
    multiColumn: ['여러 단을 읽는 순서에 맞춰 읽었어요', '여러 단을 읽는 순서에 맞춰 읽고'],
    ruledTable: ['표의 선을 따라 칸을 나눠 읽었어요', '표의 선을 따라 칸을 나눠 읽고'],
    horizontalTable: ['표의 가로선을 따라 칸을 나눠 읽었어요', '표의 가로선을 따라 칸을 나눠 읽고'],
    borderlessTable: ['선이 없는 표를 글자 간격으로 나눠 읽었어요', '선이 없는 표를 글자 간격으로 나눠 읽고'],
    nativeTable: ['표를 문서에 든 모양 그대로 읽었어요', '표를 문서에 든 모양 그대로 읽고'],
    other: ['읽었어요', '읽고'],
  },
  visualRegion: '그림 부분을 그림·차트로 따로 모았어요',
  fullPage: (noun: string) => `${noun} 전체를 그림으로 따로 모았어요`,
  skipped: '꾸밈 요소로 보고 건너뛰었어요',
  blankHow: '읽을 내용이 없었어요',
  visualTail: '그림은 그림·차트로 따로 모았어요',
  /** "표가 많은 쪽이라 표를 문서에 든 모양 그대로 읽었어요". */
  phrase: (why: string | null, how: string) => (why ? `${withCopula(why)} ${how}` : how),
  /** "1~2쪽은 " + phrase. */
  sentence: (where: string, phrase: string) => `${withParticle(where, '은/는')} ${phrase}`,
} as const;

export const listCopy = {
  heading: (count: number) => `파일 ${count}개`,
  /** What a screen reader says for one file in the list. */
  option: (name: string, meta: string) => `${name}, ${meta}`,
  queued: '기다리는 중',
  reading: '읽는 중',
  unsupported: '읽을 수 없어요',
  failed: '읽지 못했어요',
  tables: (count: number) => `표 ${count}개`,
} as const;

export const settingsCopy = {
  summary: '세부 설정 (기본값으로도 충분해요)',
  summaryChanged: (count: number) => `세부 설정 (바꾼 설정 ${count}개)`,
  pagesLabel: '일부 쪽만 읽기 (선택)',
  pagesDescription: '예: 1-3, 5 · 비워 두면 모두 읽어요. 파워포인트는 슬라이드, 엑셀은 시트 번호로 적어요.',
  pagesInvalid: (token: string) =>
    `${withParticle(quoted(token), '은/는')} 쪽 번호로 읽을 수 없어요. 1-3, 5처럼 숫자와 -, 쉼표로 적어 주세요.`,
  commentsLabel: '메모·주석 함께 가져오기',
  cropsLabel: '그림·차트 이미지 따로 저장하기',
  chunksLabel: 'AI용 문단으로 나눠 저장하기',
  layoutLabel: '스캔한 쪽에서 글·표·그림 구역 자동으로 찾기',
  layoutDescription: '스캔한 PDF처럼 그림으로만 된 쪽에 써요. 읽는 시간이 조금 늘어요.',
  chunkMaxLabel: 'AI용 문단 최대 글자 수',
  chunkMaxDescription: '200~20,000 · 기본 1,500',
  chunkOverlapLabel: '문단 겹침 글자 수',
  chunkOverlapDescription: '0~2,000 · 기본 150 · 앞뒤 문단에 함께 넣는 글자 수예요',
  numberNote: '범위를 벗어난 숫자는 가장 가까운 값으로 맞춰요.',
} as const;

export const rereadCopy = {
  changed: (name: string) =>
    `설정을 바꿨어요. ${quoted(name)}에 새 설정을 쓰려면 [다시 읽기]를 눌러 주세요.`,
  button: '다시 읽기',
} as const;

export const resultCopy = {
  viewsLabel: '결과 보기 방식',
  body: '본문',
  tables: (count: number) => `표 ${count}개`,
  visuals: (count: number) => `그림·차트 ${count}개`,
  chunks: 'AI용 문단',
  markdown: '마크다운으로 보기',
  meta: (formatName: string, sizeText: string) => `${formatName} · ${sizeText}`,
  queued: '차례를 기다리는 중이에요. 앞의 파일을 다 읽으면 바로 읽어요.',
  reading: '읽는 중이에요. 다 읽으면 여기에 본문과 표가 나와요.',
  emptyBody: '이 파일에서 읽을 글을 찾지 못했어요.',
  noTables: '이 파일에서 표를 찾지 못했어요.',
  noVisuals: '이 파일에서 그림·차트를 찾지 못했어요.',
  chunksOff:
    'AI용 문단을 만들지 않았어요. 세부 설정에서 ‘AI용 문단으로 나눠 저장하기’를 켜고 다시 읽으면 볼 수 있어요.',
  noChunks: 'AI용 문단으로 나눌 글이 없었어요.',
  chunksCapped: (shown: number, total: number) =>
    `문단 ${total}개 중 앞의 ${shown}개만 보여 줘요. 모두 ZIP에 들어 있어요.`,
  chunkMeta: (number: number, where: string, chars: number) => `문단 ${number} · ${where} · ${chars}자`,
  tableName: (number: number) => `표 ${number}`,
  tableMeta: (where: string, rows: number, columns: number) => `${where} · ${rows}행 × ${columns}열`,
  continued: '앞 표에서 이어져요',
} as const;

export const visualCopy = {
  kinds: {
    figure_image: '그림',
    vector_diagram: '도식·차트',
    scanned_page: '스캔한 쪽',
    table: '표 그림',
    formula_or_code: '수식·코드',
  } as Readonly<Record<string, string>>,
  defaultKind: '그림',
  alt: (where: string, kind: string) => `${where}의 ${kind}`,
  meta: (where: string, kind: string) => `${where} · ${kind}`,
  placeholder: (kind: string, caption: string | null) => (caption ? `[${kind}: ${caption}]` : `[${kind}]`),
  missing: {
    off: '그림·차트 이미지를 따로 저장하지 않았어요. 세부 설정에서 켜고 다시 읽으면 볼 수 있어요.',
    tooLarge: '이미지가 커서 화면에는 보여 주지 않아요. ZIP 안에 들어 있어요.',
    renderer: '이 PC에 그림을 이미지로 바꾸는 도구가 없어서 잘라 두지 못했어요.',
    failed: '그림을 이미지로 잘라 두지 못했어요.',
  },
  reason: '자세히 (판단 근거)',
} as const;

export const evidenceCopy = {
  title: '어떻게 읽었나요',
  method: '읽은 방법',
  found: '찾은 것',
  partial: (count: string, ofTotal: string) => `${count}(${ofTotal})`,
  chunks: (count: number) => `AI용 문단 ${count}개`,
  tables: (count: number) => `표 ${count}개`,
  visuals: (count: number) => `그림·차트 ${count}개`,
  notes: '참고할 점',
  notesLead: (count: number) => `읽으면서 남긴 알림이 ${count}개 있어요.`,
  notesDetails: '자세히 (알림 원문)',
  report: '자세히 (처리 기록)',
  pending: '다 읽으면 쪽마다 어떻게 읽었는지 여기에 알려 드려요.',
  supportedTitle: '읽을 수 있는 파일',
} as const;

export const exportCopy = {
  contents: (parts: readonly string[]) => `ZIP 안: ${parts.join(' · ')}`,
  parts: {
    body: '본문',
    tables: '표(CSV·엑셀)',
    images: '그림',
    chunks: 'AI용 문단',
    report: '처리 기록',
  },
  copyBody: '본문 복사하기',
  saveZip: 'ZIP으로 저장하기',
  copied: '본문을 복사했어요. 붙여 넣으면 돼요.',
  copyFailed: '본문을 복사하지 못했어요. 본문을 직접 골라 복사해 주세요.',
  /** The server always names the ZIP `…_autoetl_bundle.zip`, and "zip" is read 집, so 으로. */
  saveStarted: (fileName: string) => `${quoted(fileName)}으로 저장해요. 보통 “다운로드” 폴더에 들어가요.`,
  expired: '이 파일의 ZIP은 더 이상 남아 있지 않아요. 다시 읽으면 새로 만들어요.',
  zipMissing: 'ZIP 파일이 남아 있지 않아 저장하지 못했어요.',
  zipFailed: 'ZIP 파일을 받지 못했어요. AutoETL이 켜져 있는지 확인하고 다시 눌러 주세요.',
} as const;

/** Errors only this tab raises. Each has 무슨 일 · 왜 · 어떻게. */
export const extractErrorCopy = {
  empty: {
    what: '빈 파일이라 읽을 내용이 없어요',
    why: '이 파일은 크기가 0바이트예요. 저장하다 멈췄거나 내용 없이 만들어진 파일일 수 있어요.',
    how: '원래 프로그램에서 파일을 열어 내용을 확인하고, 다시 저장한 뒤 넣어 주세요.',
  },
  fileRead: {
    what: '파일을 열지 못했어요',
    why: '파일을 고른 뒤에 옮겨졌거나 지워졌을 수 있어요. 다른 프로그램이 파일을 쓰고 있어도 그래요.',
    how: '파일이 그 자리에 있는지 확인한 뒤 다시 넣어 주세요.',
  },
  pageSpec: {
    what: '고른 쪽 번호를 알아보지 못했어요',
    why: (token: string) => `${withParticle(quoted(token), '은/는')} 쪽 번호로 읽을 수 없어요.`,
    how: '세부 설정의 ‘일부 쪽만 읽기’에 1-3, 5처럼 숫자와 -, 쉼표로 적은 뒤 [다시 읽기]를 눌러 주세요.',
  },
  pageRange: {
    what: '고른 쪽이 이 파일에 없어요',
    why: {
      page: (last: number) => `이 파일은 ${last}쪽까지 있어요.`,
      slide: (last: number) => `이 파일의 슬라이드는 ${last}번까지 있어요.`,
      sheet: (last: number) => `이 파일의 시트는 ${last}번까지 있어요.`,
    },
    how: '세부 설정의 ‘일부 쪽만 읽기’를 고치거나 비운 뒤 [다시 읽기]를 눌러 주세요.',
  },
  encrypted: {
    what: '암호가 걸린 파일이라 읽지 못했어요',
    why: '암호로 잠근 파일은 안의 내용을 꺼낼 수 없어요.',
    how: '원래 프로그램에서 암호를 푼 뒤 다시 저장해 넣어 주세요.',
  },
  tooLargeJustOver: (limitText: string) =>
    `이 파일은 ${withParticle(limitText, '을/를')} 조금 넘어요. 한 번에 넣을 수 있는 크기는 ${limitText}까지예요.`,
  detail: {
    extension: (name: string, extension: string, allowed: string) =>
      `파일 이름: ${name}\n확장자: ${extension || '(없음)'}\n읽을 수 있는 확장자: ${allowed}`,
    size: (name: string, size: number, maxBytes: number) =>
      `파일 이름: ${name}\n파일 크기: ${size}바이트\n한도: ${maxBytes}바이트`,
    empty: (name: string) => `파일 이름: ${name}\n파일 크기: 0바이트`,
    noBundleId: 'POST /api/parse: 응답에 bundle_id가 없어요.',
  },
  actions: {
    pickOther: '다른 파일 넣기',
    remove: '목록에서 빼기',
    retry: '다시 읽기',
  },
} as const;
