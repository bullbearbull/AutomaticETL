import { withParticle } from '../lib/korean.ts';

/**
 * Shared UI text. Every sentence uses 해요체; short names (buttons, tabs,
 * table headers) are nouns or "무엇을 + 하기". Text that only one tab uses
 * belongs in that tab's own `copy.ts`.
 */

export const appCopy = {
  name: 'AutoETL',
  documentTitle: 'AutoETL',
  trust: '내 PC에서만 실행 · 외부 전송 없음',
  tabListLabel: '기능',
  loadingTab: '화면을 불러오는 중이에요',
} as const;

/** Tab ids in display order. `settings` sits at the right end. */
export const tabIds = ['extract', 'database', 'search', 'settings'] as const;
export type TabId = (typeof tabIds)[number];

export const tabLabels: Record<TabId, string> = {
  extract: '문서 글·표 추출',
  database: '엑셀·CSV로 DB 만들기',
  search: '문서 내용 검색',
  settings: '설정',
};

/** The same three steps on every tab. Never call progress messages "단계". */
export const stepLabels = ['넣기', '확인·다듬기', '내보내기'] as const;

export const stepCopy = {
  listLabel: '진행 순서',
  done: '끝남',
  current: '지금',
} as const;

export const buttonCopy = {
  close: '닫기',
  cancel: '취소',
  pickFiles: '파일 고르기',
  addFiles: '+ 파일 넣기',
  copyForExcel: '엑셀용으로 복사하기',
  details: '자세히',
} as const;

export const dropZoneCopy = {
  label: '파일 넣는 곳',
  prompt: '여기에 파일을 끌어 놓거나',
  compactHint: '목록에 끌어 놓아도 돼요',
} as const;

export const workspaceCopy = {
  list: '목록',
  result: '결과',
  evidence: '근거',
  showEvidence: '근거 보기',
} as const;

export const statusCopy = {
  ready: '준비됐어요',
  elapsed: (seconds: number) => `${seconds}초`,
  error: '오류',
  warning: '주의',
  done: '완료',
} as const;

export const tableCopy = {
  copied: (tableName: string) => `${withParticle(tableName, '을/를')} 복사했어요. 엑셀에 붙여 넣으면 돼요.`,
  copyFailed: (tableName: string) =>
    `${withParticle(tableName, '을/를')} 복사하지 못했어요. 표를 직접 골라 복사해 주세요.`,
  scrollRegion: (tableName: string) => `${tableName}, 옆으로 밀어 볼 수 있어요`,
  empty: '표에 내용이 없어요.',
} as const;

export const comingSoonCopy = {
  body: '이 기능은 새 화면에서 준비 중이에요. 지금은 기존 화면에서 쓸 수 있어요.',
  link: '기존 화면에서 열기',
} as const;

/** A tab whose screen could not be loaded, or stopped with an error. */
export const tabErrorCopy = {
  notice: {
    what: '이 탭을 열지 못했어요',
    why: 'AutoETL이 꺼졌거나, 이 탭 화면을 불러오다 문제가 생겼어요.',
    how: 'AutoETL이 켜져 있는지 확인하고 [다시 불러오기]를 눌러 주세요. 다른 탭의 내용은 그대로 있어요.',
  },
  retry: '다시 불러오기',
} as const;

export const markdownCopy = {
  image: (alt: string) => (alt ? `[그림: ${alt}]` : '[그림]'),
  tableRegion: '본문 표, 옆으로 밀어 볼 수 있어요',
} as const;

/** Labels for the 무슨 일 · 왜 · 어떻게 error template. */
export const errorTemplateCopy = {
  why: '왜',
  how: '어떻게',
  rawDetail: '자세히 (프로그램이 보낸 원래 글)',
} as const;

/**
 * Shared error messages (무슨 일 · 왜 · 어떻게). Server text is never shown
 * as the message; it goes under 자세히.
 */
export const errorCopy = {
  network: {
    what: 'AutoETL과 연결이 끊겼어요',
    why: '이 화면과 AutoETL 사이의 연결이 닫혔어요.',
    how: 'AutoETL을 실행한 창이 닫혔다면 다시 실행하고, 이 화면을 새로 고쳐 주세요.',
  },
  forbidden: {
    what: 'AutoETL이 이 요청을 막았어요',
    why: '이 화면을 AutoETL이 연 주소와 다른 주소로 열면, 안전을 위해 요청을 받지 않아요.',
    how: 'AutoETL을 실행한 창에 나온 주소(예: http://127.0.0.1:8765)로 이 화면을 다시 열어 주세요.',
  },
  tooLarge: {
    what: '파일이 너무 커서 읽을 수 없어요',
    why: '한 번에 넣을 수 있는 크기는 50MB까지예요.',
    how: '필요한 쪽만 따로 저장해 나눠 넣어 주세요.',
  },
  expired: {
    what: '분석 결과가 더 이상 남아 있지 않아요',
    why: 'AutoETL은 최근 분석 결과 몇 개만 남기고, 다시 켜면 모두 지워요.',
    how: '파일을 다시 넣어 주세요.',
  },
  unreadable: {
    what: '이 파일을 읽지 못했어요',
    why: '파일이 손상됐거나, AutoETL이 아직 읽지 못하는 내용이 들어 있을 수 있어요.',
    how: '파일을 원래 프로그램에서 열어 다시 저장한 뒤 넣어 주세요.',
  },
  request: {
    what: '요청을 처리하지 못했어요',
    why: 'AutoETL이 이 화면에서 보낸 요청을 받아들이지 않았어요.',
    how: '화면을 새로 고친 뒤 다시 해 주세요. 계속되면 자세히에 있는 원래 글을 함께 알려 주세요.',
  },
  server: {
    what: '처리하는 중에 문제가 생겼어요',
    why: 'AutoETL 안에서 예상하지 못한 오류가 났어요.',
    how: '잠시 뒤 다시 해 주세요. 계속되면 AutoETL을 다시 실행해 주세요.',
  },
  aborted: {
    what: '요청을 멈췄어요',
    why: '작업을 취소했거나 다른 작업을 시작했어요.',
    how: '필요하면 다시 시작해 주세요.',
  },
  unsupportedFile: {
    what: '이 파일은 읽을 수 없어요',
    why: (ext: string) =>
      ext ? `${ext} 파일은 아직 지원하지 않아요.` : '확장자가 없는 파일은 읽을 수 없어요.',
    whyWithList: (ext: string, supported: string) =>
      ext
        ? `${ext} 파일은 아직 지원하지 않아요. 읽을 수 있는 형식은 ${supported}예요.`
        : `확장자가 없는 파일은 읽을 수 없어요. 읽을 수 있는 형식은 ${supported}예요.`,
    how: '읽을 수 있는 형식으로 다시 저장한 뒤 넣어 주세요.',
    xlsWhy: '구형 엑셀(.xls) 파일은 아직 지원하지 않아요.',
    xlsHow:
      '엑셀에서 파일을 연 뒤 [다른 이름으로 저장]에서 파일 형식을 "Excel 통합 문서(*.xlsx)"로 골라 저장하고, 그 파일을 넣어 주세요.',
  },
  fileTooLarge: {
    what: '파일이 너무 커서 읽을 수 없어요',
    why: (sizeText: string, limitText: string) =>
      `이 파일은 ${sizeText}이고, 한 번에 넣을 수 있는 크기는 ${limitText}까지예요.`,
    how: '필요한 쪽만 따로 저장해 나눠 넣어 주세요.',
  },
} as const;
