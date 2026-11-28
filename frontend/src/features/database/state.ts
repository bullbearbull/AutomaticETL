import type { AppError } from '../../lib/errors.ts';
import type { AnalyzeResult, DbEngine, LoadDbResult, OrphanPolicy, Purpose } from './api.ts';
import { statusText } from './copy.ts';
import {
  hasInvalidTargetColumnNames,
  pruneOverridesForTables,
  type ColumnOverrideDraft,
} from './overrides.ts';
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
  decomposeStrings: true,
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
  /** Monotonic revision of the current `files` list; bumped whenever files change. */
  fileRevision: number;
  /** The `fileRevision` that produced `analysis`, or null if no analysis matches. */
  analyzedFileRevision: number | null;
  /** Monotonic revision bumped on file changes, override changes, or analysis-affecting settings. */
  editRevision: number;
  /** Blocking error shown when no analysis result is available. */
  error: AppError | null;
  /** Non-blocking alert banner shown while keeping an existing analysis on screen. */
  noticeError: AppError | null;
  savingDb: boolean;
  loadOutcome: LoadDbResult | null;
  /** True only after a DB download or ZIP download succeeded for the current revision. */
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
  fileRevision: 0,
  analyzedFileRevision: null,
  editRevision: 0,
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
  | { type: 'analyzeSucceeded'; result: AnalyzeResult; fileRevision: number; now: number }
  | { type: 'analyzeFailed'; error: AppError; now: number }
  | { type: 'startSaveDb'; now: number }
  | {
      type: 'saveDbSucceeded';
      outcome: LoadDbResult;
      fileRevision: number;
      editRevision: number;
      now: number;
    }
  | { type: 'saveDbFailed'; error: AppError; now: number }
  | { type: 'markExported'; fileRevision: number; editRevision: number }
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

/** True when `analysis` is absent or came from an older `fileRevision` than `files`. */
export function isAnalysisStale(state: DatabaseState): boolean {
  return state.analysis === null || state.analyzedFileRevision !== state.fileRevision;
}

/**
 * True only when `analysis` matches the current `files`, analysis/revalidation
 * is settled (`phase === 'ready'`), no save is in flight, and no active DB
 * column name has a syntax error.
 */
export function canSaveDatabase(state: DatabaseState): boolean {
  return (
    state.analysis !== null &&
    !isAnalysisStale(state) &&
    state.phase === 'ready' &&
    !state.savingDb &&
    !hasInvalidTargetColumnNames(state.overrides, state.analysis)
  );
}

export function databaseReducer(state: DatabaseState, action: DatabaseAction): DatabaseState {
  switch (action.type) {
    case 'mergeFiles':
      return {
        ...state,
        files: mergeFileLists(state.files, action.items),
        fileRevision: state.fileRevision + 1,
        editRevision: state.editRevision + 1,
        error: null,
        noticeError: null,
        loadOutcome: null,
        exported: false,
        bundleExpired: false,
      };
    case 'removeFile': {
      const removed = state.files.find((f) => f.id === action.id);
      const remaining = state.files.filter((f) => f.id !== action.id);
      if (remaining.length === 0) {
        return {
          ...initialDatabaseState,
          settings: state.settings,
          fileRevision: state.fileRevision + 1,
          editRevision: state.editRevision + 1,
        };
      }
      const removedTables = new Set<string>();
      if (removed && state.analysis) {
        for (const t of state.analysis.tables) {
          if (
            t.source.sourcePath === removed.name ||
            t.source.displayName === removed.name ||
            t.source.displayName.startsWith(`${removed.name} `)
          ) {
            removedTables.add(t.source.tableName);
          }
        }
      }
      return {
        ...state,
        files: remaining,
        overrides: pruneOverridesForTables(state.overrides, removedTables),
        fileRevision: state.fileRevision + 1,
        editRevision: state.editRevision + 1,
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
        fileRevision: state.fileRevision + 1,
        editRevision: state.editRevision + 1,
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
        ...(affectsAnalysis || action.patch.dbEngine !== undefined || action.patch.notes !== undefined
          ? {
              editRevision: state.editRevision + 1,
              loadOutcome: null,
              exported: false,
            }
          : {}),
      };
    }
    case 'setOverrides':
      return {
        ...state,
        overrides: action.overrides,
        editRevision: state.editRevision + 1,
        noticeError: null,
        loadOutcome: null,
        exported: false,
      };
    case 'resetOverrides':
      return {
        ...state,
        overrides: {},
        editRevision: state.editRevision + 1,
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
      // Discard results belonging to a superseded file set.
      if (action.fileRevision !== state.fileRevision) {
        return state;
      }
      return {
        ...state,
        analysis: action.result,
        analyzedFileRevision: action.fileRevision,
        selectedTable: resolveSelectedTable(state.selectedTable, action.result),
        phase: 'ready',
        error: null,
        noticeError: null,
        bundleExpired: false,
        endedAt: action.now,
      };
    case 'analyzeFailed':
      if (state.analysis) {
        // Keep the previous analysis for reference while `analyzedFileRevision !== fileRevision` blocks saving if stale.
        return {
          ...state,
          phase: isAnalysisStale(state) ? 'error' : 'ready',
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
      if (action.fileRevision !== state.fileRevision || action.editRevision !== state.editRevision) {
        return { ...state, savingDb: false, endedAt: action.now };
      }
      return {
        ...state,
        savingDb: false,
        loadOutcome: action.outcome,
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
      if (action.fileRevision !== state.fileRevision || action.editRevision !== state.editRevision) {
        return state;
      }
      return { ...state, exported: true };
    case 'markBundleExpired':
      return { ...state, bundleExpired: true };
    default:
      return state;
  }
}

export function stepFor(state: DatabaseState): StepNumber {
  if (!state.analysis) return 1;
  if (state.exported && !isAnalysisStale(state)) return 3;
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
  if (state.analysis && isAnalysisStale(state)) {
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
  if (state.loadOutcome?.loadResult.status === 'failed') {
    return {
      message: statusText.error,
      tone: 'error',
      startedAt: state.startedAt,
      endedAt: state.endedAt,
    };
  }
  if (state.loadOutcome) {
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
