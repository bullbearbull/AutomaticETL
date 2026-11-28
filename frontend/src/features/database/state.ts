import type { AppError } from '../../lib/errors.ts';
import type { AnalyzeResult, DbEngine, LoadDbResult, OrphanPolicy, Purpose } from './api.ts';
import { statusText } from './copy.ts';
import type { ColumnOverrideDraft } from './overrides.ts';
import { unmaskedPiiColumns } from './pii.ts';

/** Matches StatusBar `tone` and StepIndicator `current` without pulling JSX into unit tests. */
export type StatusTone = 'idle' | 'busy' | 'done' | 'warning' | 'error';
export type StepNumber = 1 | 2 | 3;

export interface DatabaseFileItem {
  id: string;
  file: File;
  name: string;
  size: number;
}

export interface DatabaseSettings {
  purpose: Purpose;
  decomposeStrings: boolean;
  dbEngine: DbEngine;
  orphanPolicy: OrphanPolicy;
  includeAuditColumns: boolean;
  includeManagementTables: boolean;
  notes: string;
}

export const DEFAULT_DATABASE_SETTINGS: DatabaseSettings = {
  purpose: 'oltp',
  decomposeStrings: false,
  dbEngine: 'sqlite',
  orphanPolicy: 'quarantine',
  includeAuditColumns: true,
  includeManagementTables: true,
  notes: '',
};

export type AnalyzePhase = 'idle' | 'analyzing' | 'revalidating' | 'ready' | 'error';

export interface DatabaseState {
  files: DatabaseFileItem[];
  settings: DatabaseSettings;
  overrides: Record<string, ColumnOverrideDraft>;
  analysis: AnalyzeResult | null;
  selectedTable: string | null;
  phase: AnalyzePhase;
  /** Blocking error shown when no analysis result is available. */
  error: AppError | null;
  /** Non-blocking alert banner shown while keeping an existing analysis on screen. */
  noticeError: AppError | null;
  savingDb: boolean;
  loadOutcome: LoadDbResult | null;
  /** True once a DB file or design ZIP has been saved during the current analysis. */
  exported: boolean;
  bundleExpired: boolean;
  startedAt: number | null;
  endedAt: number | null;
}

export const initialDatabaseState: DatabaseState = {
  files: [],
  settings: DEFAULT_DATABASE_SETTINGS,
  overrides: {},
  analysis: null,
  selectedTable: null,
  phase: 'idle',
  error: null,
  noticeError: null,
  savingDb: false,
  loadOutcome: null,
  exported: false,
  bundleExpired: false,
  startedAt: null,
  endedAt: null,
};

export type DatabaseAction =
  | { type: 'mergeFiles'; items: DatabaseFileItem[] }
  | { type: 'removeFile'; id: string }
  | { type: 'clearAll' }
  | { type: 'setValidationError'; error: AppError }
  | { type: 'dismissNoticeError' }
  | { type: 'setSettings'; patch: Partial<DatabaseSettings> }
  | { type: 'setOverrides'; overrides: Record<string, ColumnOverrideDraft> }
  | { type: 'resetOverrides' }
  | { type: 'selectTable'; tableName: string }
  | { type: 'startAnalyze'; mode: 'initial' | 'revalidate'; now: number }
  | { type: 'analyzeSucceeded'; result: AnalyzeResult; now: number }
  | { type: 'analyzeFailed'; error: AppError; now: number }
  | { type: 'startSaveDb'; now: number }
  | { type: 'saveDbSucceeded'; outcome: LoadDbResult; now: number }
  | { type: 'saveDbFailed'; error: AppError; now: number }
  | { type: 'markExported' }
  | { type: 'markBundleExpired' };

function mergeFileLists(
  existing: readonly DatabaseFileItem[],
  added: readonly DatabaseFileItem[],
): DatabaseFileItem[] {
  const next = [...existing];
  for (const item of added) {
    const idx = next.findIndex((cur) => cur.name === item.name);
    if (idx >= 0) next[idx] = item;
    else next.push(item);
  }
  return next;
}

function resolveSelectedTable(previous: string | null, analysis: AnalyzeResult): string | null {
  if (previous && analysis.tables.some((t) => t.source.tableName === previous)) {
    return previous;
  }
  return analysis.tables[0]?.source.tableName ?? null;
}

export function databaseReducer(state: DatabaseState, action: DatabaseAction): DatabaseState {
  switch (action.type) {
    case 'mergeFiles':
      return {
        ...state,
        files: mergeFileLists(state.files, action.items),
        error: null,
        noticeError: null,
        loadOutcome: null,
        exported: false,
        bundleExpired: false,
      };
    case 'removeFile': {
      const remaining = state.files.filter((f) => f.id !== action.id);
      if (remaining.length === 0) {
        return {
          ...initialDatabaseState,
          settings: state.settings,
        };
      }
      return {
        ...state,
        files: remaining,
        error: null,
        noticeError: null,
        loadOutcome: null,
        exported: false,
        bundleExpired: false,
      };
    }
    case 'clearAll':
      return {
        ...initialDatabaseState,
        settings: state.settings,
      };
    case 'setValidationError':
      return state.analysis
        ? { ...state, noticeError: action.error, endedAt: Date.now() }
        : { ...state, phase: 'error', error: action.error, startedAt: null, endedAt: null };
    case 'dismissNoticeError':
      return { ...state, noticeError: null };
    case 'setSettings': {
      const affectsAnalysis =
        action.patch.purpose !== undefined ||
        action.patch.decomposeStrings !== undefined ||
        action.patch.orphanPolicy !== undefined ||
        action.patch.includeAuditColumns !== undefined ||
        action.patch.includeManagementTables !== undefined;
      return {
        ...state,
        settings: { ...state.settings, ...action.patch },
        ...(affectsAnalysis ? { loadOutcome: null, exported: false } : {}),
      };
    }
    case 'setOverrides':
      return {
        ...state,
        overrides: action.overrides,
        noticeError: null,
        loadOutcome: null,
        exported: false,
      };
    case 'resetOverrides':
      return {
        ...state,
        overrides: {},
        noticeError: null,
        loadOutcome: null,
        exported: false,
      };
    case 'selectTable':
      return { ...state, selectedTable: action.tableName };
    case 'startAnalyze':
      return {
        ...state,
        phase: action.mode === 'revalidate' && state.analysis ? 'revalidating' : 'analyzing',
        error: null,
        noticeError: null,
        bundleExpired: false,
        startedAt: action.now,
        endedAt: null,
      };
    case 'analyzeSucceeded':
      return {
        ...state,
        analysis: action.result,
        selectedTable: resolveSelectedTable(state.selectedTable, action.result),
        phase: 'ready',
        error: null,
        noticeError: null,
        bundleExpired: false,
        endedAt: action.now,
      };
    case 'analyzeFailed':
      if (state.analysis) {
        return {
          ...state,
          phase: 'ready',
          noticeError: action.error,
          endedAt: action.now,
        };
      }
      return {
        ...state,
        phase: 'error',
        error: action.error,
        endedAt: action.now,
      };
    case 'startSaveDb':
      return {
        ...state,
        savingDb: true,
        noticeError: null,
        startedAt: action.now,
        endedAt: null,
      };
    case 'saveDbSucceeded':
      return {
        ...state,
        savingDb: false,
        loadOutcome: action.outcome,
        exported: action.outcome.loadResult.status !== 'failed',
        endedAt: action.now,
      };
    case 'saveDbFailed':
      return {
        ...state,
        savingDb: false,
        noticeError: action.error,
        endedAt: action.now,
      };
    case 'markExported':
      return { ...state, exported: true };
    case 'markBundleExpired':
      return { ...state, bundleExpired: true };
    default:
      return state;
  }
}

export function stepFor(state: DatabaseState): StepNumber {
  if (!state.analysis) return 1;
  if (state.exported) return 3;
  return 2;
}

export interface DatabaseStatusSummary {
  message: string;
  tone: StatusTone;
  startedAt: number | null;
  endedAt: number | null;
}

export function statusFor(state: DatabaseState): DatabaseStatusSummary {
  if (state.savingDb) {
    return {
      message: statusText.savingDb(state.settings.dbEngine),
      tone: 'busy',
      startedAt: state.startedAt,
      endedAt: null,
    };
  }
  if (state.phase === 'analyzing') {
    return {
      message: statusText.analyzing(state.files.length),
      tone: 'busy',
      startedAt: state.startedAt,
      endedAt: null,
    };
  }
  if (state.phase === 'revalidating') {
    return {
      message: statusText.revalidating,
      tone: 'busy',
      startedAt: state.startedAt,
      endedAt: null,
    };
  }
  if (state.error && !state.analysis) {
    return {
      message: statusText.error,
      tone: 'error',
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
  if (state.loadOutcome && state.loadOutcome.loadResult.status !== 'failed') {
    return {
      message: statusText.saved(
        state.loadOutcome.downloadFilename,
        state.loadOutcome.loadResult.totalRowsLoaded,
      ),
      tone: state.loadOutcome.loadResult.status === 'partial' ? 'warning' : 'done',
      startedAt: state.startedAt,
      endedAt: state.endedAt,
    };
  }
  if (state.analysis) {
    const unmasked = unmaskedPiiColumns(state.analysis, state.overrides);
    if (unmasked.length > 0 && state.settings.purpose !== 'archive') {
      return {
        message: statusText.readyUnmaskedWarning(state.analysis.tables.length, unmasked.length),
        tone: 'warning',
        startedAt: state.startedAt,
        endedAt: state.endedAt,
      };
    }
    return {
      message: statusText.ready(
        state.analysis.tables.length,
        state.analysis.relations.foreignKeys.length,
        state.analysis.elapsedSeconds,
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
