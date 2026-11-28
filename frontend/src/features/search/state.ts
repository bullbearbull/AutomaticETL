import type { AppError } from '../../lib/errors.ts';
import type {
  DocIndexBuildResult,
  RerankerKind,
  SearchEvidencePack,
} from './api.ts';
import { statusText } from './copy.ts';

export type StatusTone = 'idle' | 'busy' | 'done' | 'warning' | 'error';
export type StepNumber = 1 | 2 | 3;

export interface SearchFileItem {
  id: string;
  file: File;
  name: string;
  size: number;
}

export interface SearchSettings {
  topK: number;
  synonyms: string;
  reranker: RerankerKind;
  rerankerModel: string;
}

export const DEFAULT_SEARCH_SETTINGS: SearchSettings = {
  topK: 5,
  synonyms: '',
  reranker: 'none',
  rerankerModel: '',
};

export type BuildPhase = 'idle' | 'building' | 'ready' | 'error';

export interface SearchState {
  files: SearchFileItem[];
  settings: SearchSettings;
  index: DocIndexBuildResult | null;
  buildPhase: BuildPhase;
  /** Monotonic revision bumped whenever `files` change. */
  fileRevision: number;
  /** The `fileRevision` that built `index`, or null when no index matches. */
  indexedFileRevision: number | null;
  /** Blocking error shown when no built index is available. */
  error: AppError | null;
  /** Non-blocking banner shown while keeping the previous index visible. */
  noticeError: AppError | null;
  /** Empty string searches all indexed documents; otherwise restricts to one `docId`. */
  docFilterId: string;
  query: string;
  searching: boolean;
  searchResult: SearchEvidencePack | null;
  selectedHitId: string | null;
  /** True after [찾은 부분 복사하기] or table copy succeeded for the active revision. */
  copied: boolean;
  startedAt: number | null;
  endedAt: number | null;
}

export const initialSearchState: SearchState = {
  files: [],
  settings: DEFAULT_SEARCH_SETTINGS,
  index: null,
  buildPhase: 'idle',
  fileRevision: 0,
  indexedFileRevision: null,
  error: null,
  noticeError: null,
  docFilterId: '',
  query: '',
  searching: false,
  searchResult: null,
  selectedHitId: null,
  copied: false,
  startedAt: null,
  endedAt: null,
};

export type SearchAction =
  | { type: 'mergeFiles'; items: SearchFileItem[] }
  | { type: 'removeFile'; id: string }
  | { type: 'clearAll' }
  | { type: 'setValidationError'; error: AppError }
  | { type: 'dismissNoticeError' }
  | { type: 'setSettings'; patch: Partial<SearchSettings> }
  | { type: 'setDocFilter'; docId: string }
  | { type: 'setQuery'; query: string }
  | { type: 'startBuild'; now: number }
  | { type: 'buildSucceeded'; result: DocIndexBuildResult; fileRevision: number; now: number }
  | { type: 'buildFailed'; error: AppError; fileRevision: number; now: number }
  | { type: 'startSearch'; query: string; now: number }
  | { type: 'searchSucceeded'; result: SearchEvidencePack; fileRevision: number; now: number }
  | { type: 'searchFailed'; error: AppError; fileRevision: number; now: number }
  | { type: 'cancelSearch'; now: number }
  | { type: 'selectHit'; sectionId: string }
  | { type: 'markCopied'; fileRevision: number };

function mergeFileLists(
  existing: readonly SearchFileItem[],
  added: readonly SearchFileItem[],
): SearchFileItem[] {
  const next = [...existing];
  for (const item of added) {
    const idx = next.findIndex((cur) => cur.name === item.name);
    if (idx >= 0) next[idx] = item;
    else next.push(item);
  }
  return next;
}

/** True when no index exists or `files` changed since the current index was built. */
export function isIndexStale(state: SearchState): boolean {
  return state.index === null || state.indexedFileRevision !== state.fileRevision;
}

/** True only when an up-to-date search index is ready for querying. */
export function canSearch(state: SearchState): boolean {
  return state.index !== null && !isIndexStale(state) && state.buildPhase === 'ready';
}

export function searchReducer(state: SearchState, action: SearchAction): SearchState {
  switch (action.type) {
    case 'mergeFiles':
      return {
        ...state,
        files: mergeFileLists(state.files, action.items),
        fileRevision: state.fileRevision + 1,
        error: null,
        noticeError: null,
        searching: false,
        docFilterId: '',
        searchResult: null,
        selectedHitId: null,
        copied: false,
      };
    case 'removeFile': {
      const remaining = state.files.filter((f) => f.id !== action.id);
      if (remaining.length === 0) {
        return {
          ...initialSearchState,
          settings: state.settings,
          query: state.query,
          fileRevision: state.fileRevision + 1,
        };
      }
      return {
        ...state,
        files: remaining,
        fileRevision: state.fileRevision + 1,
        error: null,
        noticeError: null,
        searching: false,
        docFilterId: '',
        searchResult: null,
        selectedHitId: null,
        copied: false,
      };
    }
    case 'clearAll':
      return {
        ...initialSearchState,
        settings: state.settings,
        fileRevision: state.fileRevision + 1,
      };
    case 'setValidationError':
      return state.index
        ? { ...state, noticeError: action.error, endedAt: Date.now() }
        : { ...state, buildPhase: 'error', error: action.error, startedAt: null, endedAt: null };
    case 'dismissNoticeError':
      return { ...state, noticeError: null };
    case 'setSettings':
      return {
        ...state,
        settings: { ...state.settings, ...action.patch },
      };
    case 'setDocFilter':
      return { ...state, docFilterId: action.docId };
    case 'setQuery':
      return { ...state, query: action.query };
    case 'startBuild':
      return {
        ...state,
        buildPhase: 'building',
        error: null,
        noticeError: null,
        startedAt: action.now,
        endedAt: null,
      };
    case 'buildSucceeded':
      if (action.fileRevision !== state.fileRevision) return state;
      return {
        ...state,
        index: action.result,
        indexedFileRevision: action.fileRevision,
        buildPhase: 'ready',
        error: null,
        noticeError: null,
        docFilterId: action.result.documents.some((d) => d.docId === state.docFilterId)
          ? state.docFilterId
          : '',
        endedAt: action.now,
      };
    case 'buildFailed':
      if (action.fileRevision !== state.fileRevision) return state;
      if (state.index) {
        return {
          ...state,
          buildPhase: 'error',
          indexedFileRevision: null,
          noticeError: action.error,
          endedAt: action.now,
        };
      }
      return {
        ...state,
        buildPhase: 'error',
        error: action.error,
        endedAt: action.now,
      };
    case 'startSearch':
      return {
        ...state,
        query: action.query,
        searching: true,
        noticeError: null,
        copied: false,
        startedAt: action.now,
        endedAt: null,
      };
    case 'searchSucceeded':
      if (action.fileRevision !== state.fileRevision) return state;
      return {
        ...state,
        searching: false,
        searchResult: action.result,
        selectedHitId: action.result.hits[0]?.sectionId ?? null,
        noticeError: null,
        endedAt: action.now,
      };
    case 'searchFailed':
      if (action.fileRevision !== state.fileRevision) {
        return state.searching ? { ...state, searching: false, endedAt: action.now } : state;
      }
      return {
        ...state,
        searching: false,
        noticeError: action.error,
        endedAt: action.now,
      };
    case 'cancelSearch':
      if (!state.searching) return state;
      return {
        ...state,
        searching: false,
        endedAt: action.now,
      };
    case 'selectHit':
      return { ...state, selectedHitId: action.sectionId };
    case 'markCopied':
      if (action.fileRevision !== state.fileRevision) return state;
      return { ...state, copied: true };
    default:
      return state;
  }
}

/** Step 1 until ready, Step 2 while searching/refining, Step 3 after copying an excerpt/table. */
export function stepFor(state: SearchState): StepNumber {
  if (!state.index) return 1;
  if (state.copied && !isIndexStale(state)) return 3;
  return 2;
}

export interface SearchStatusSummary {
  message: string;
  tone: StatusTone;
  startedAt: number | null;
  endedAt: number | null;
}

export function statusFor(state: SearchState): SearchStatusSummary {
  if (state.buildPhase === 'building') {
    return {
      message: statusText.preparing(state.files.length),
      tone: 'busy',
      startedAt: state.startedAt,
      endedAt: null,
    };
  }
  if (state.searching) {
    return {
      message: statusText.searching(state.query.trim()),
      tone: 'busy',
      startedAt: state.startedAt,
      endedAt: null,
    };
  }
  if (state.error && !state.index) {
    return {
      message: statusText.error,
      tone: 'error',
      startedAt: state.startedAt,
      endedAt: state.endedAt,
    };
  }
  if (state.index && isIndexStale(state)) {
    return {
      message: statusText.stale,
      tone: state.noticeError ? 'error' : 'warning',
      startedAt: state.startedAt,
      endedAt: state.endedAt,
    };
  }
  if (state.noticeError) {
    return {
      message: statusText.error,
      tone: 'warning',
      startedAt: state.startedAt,
      endedAt: state.endedAt,
    };
  }
  if (state.searchResult) {
    return {
      message: statusText.searched(state.searchResult.hits.length, state.searchResult.query),
      tone: state.searchResult.hits.length > 0 ? 'done' : 'warning',
      startedAt: state.startedAt,
      endedAt: state.endedAt,
    };
  }
  if (state.index) {
    return {
      message: statusText.ready(
        state.index.summary.documentsIndexed,
        state.index.summary.sectionsIndexed,
        state.index.elapsedSeconds,
      ),
      tone: 'done',
      startedAt: state.startedAt,
      endedAt: state.endedAt,
    };
  }
  return {
    message: statusText.idle,
    tone: 'idle',
    startedAt: null,
    endedAt: null,
  };
}
