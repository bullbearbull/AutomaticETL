/**
 * The tab's files and what happened to each: a reducer plus the status
 * line and step they lead to. Files are read one at a time, in list order.
 */
import type { AppError } from '../../lib/errors.ts';
import type { ParseResult } from './api.ts';
import { listCopy, statusText } from './copy.ts';
import { listMeta } from './routing.ts';
import type { ExtractSettings } from './settings.ts';

export type EntryStatus = 'queued' | 'reading' | 'done' | 'error';

/** The same values as StatusBar's `tone` and StepIndicator's `current` (kept here so tests need no JSX). */
export type StatusTone = 'idle' | 'busy' | 'done' | 'warning' | 'error';
export type StepNumber = 1 | 2 | 3;

export interface ExtractEntry {
  id: string;
  file: Blob;
  name: string;
  size: number;
  status: EntryStatus;
  /** `Date.now()` when the last read started and ended. */
  startedAt: number | null;
  endedAt: number | null;
  /** The settings the last read used; `null` before the first read. */
  settings: ExtractSettings | null;
  result: ParseResult | null;
  error: AppError | null;
  /** The user copied or saved something from this result (step 3). */
  exported: boolean;
  /** `zipCount` when this result's ZIP was made or last fetched; see isZipExpired. */
  zipMark: number;
  /** The server answered 404 for this result's ZIP. */
  zipGone: boolean;
}

export interface ExtractState {
  entries: ExtractEntry[];
  selectedId: string | null;
  /** How many ZIPs this tab has had the server make. */
  zipCount: number;
}

export type ExtractAction =
  | { type: 'add'; files: { id: string; file: Blob; name: string; size: number; error: AppError | null }[] }
  | { type: 'start'; id: string; settings: ExtractSettings; now: number }
  | { type: 'succeed'; id: string; result: ParseResult; now: number }
  | { type: 'fail'; id: string; error: AppError; now: number }
  | { type: 'retry'; id: string }
  | { type: 'remove'; id: string }
  | { type: 'select'; id: string }
  | { type: 'exported'; id: string }
  | { type: 'zipFetched'; id: string }
  | { type: 'zipMissing'; id: string };

export const initialState: ExtractState = { entries: [], selectedId: null, zipCount: 0 };

/**
 * The server keeps the last 10 ZIPs it made (`_MAX_CACHED_BUNDLES`) and drops
 * the least recently used one. Once this tab has made 10 newer ZIPs, this one
 * is surely gone. It can be gone earlier too: the classic page and other
 * tools fill the same store, and a restart empties it. So the count is
 * only an early hint; the ZIP button asks the server (zipMissing).
 */
export const SERVER_KEEPS_ZIPS = 10;

function update(state: ExtractState, id: string, change: (entry: ExtractEntry) => ExtractEntry): ExtractState {
  let found = false;
  const entries = state.entries.map((entry) => {
    if (entry.id !== id) return entry;
    found = true;
    return change(entry);
  });
  return found ? { ...state, entries } : state;
}

export function extractReducer(state: ExtractState, action: ExtractAction): ExtractState {
  switch (action.type) {
    case 'add': {
      if (action.files.length === 0) return state;
      const added: ExtractEntry[] = action.files.map((file) => ({
        id: file.id,
        file: file.file,
        name: file.name,
        size: file.size,
        status: file.error ? 'error' : 'queued',
        startedAt: null,
        endedAt: null,
        settings: null,
        result: null,
        error: file.error,
        exported: false,
        zipMark: 0,
        zipGone: false,
      }));
      const selected = state.entries.find((entry) => entry.id === state.selectedId);
      // Keep showing a result the user is looking at; otherwise show the new file.
      const selectNew = !selected || selected.status === 'error';
      return {
        ...state,
        entries: [...state.entries, ...added],
        selectedId: selectNew ? (added[0]?.id ?? state.selectedId) : state.selectedId,
      };
    }
    case 'start':
      return update(state, action.id, (entry) => ({
        ...entry,
        status: 'reading',
        startedAt: action.now,
        endedAt: null,
        settings: action.settings,
        result: null,
        error: null,
        exported: false,
      }));
    case 'succeed': {
      if (!state.entries.some((entry) => entry.id === action.id)) return state;
      const zipCount = state.zipCount + 1;
      const next = update(state, action.id, (entry) => ({
        ...entry,
        status: 'done',
        endedAt: action.now,
        result: action.result,
        error: null,
        zipMark: zipCount,
        zipGone: false,
      }));
      return { ...next, zipCount };
    }
    case 'fail':
      return update(state, action.id, (entry) => ({
        ...entry,
        status: 'error',
        endedAt: action.now,
        result: null,
        error: action.error,
      }));
    case 'retry':
      return update(state, action.id, (entry) =>
        entry.status === 'reading' || entry.status === 'queued' ? entry : { ...entry, status: 'queued', error: null },
      );
    case 'remove': {
      const index = state.entries.findIndex((entry) => entry.id === action.id);
      if (index < 0) return state;
      const entries = state.entries.filter((entry) => entry.id !== action.id);
      let selectedId = state.selectedId;
      if (selectedId === action.id) {
        // The next file takes its place; at the end of the list, the one before.
        selectedId = (entries[index] ?? entries[index - 1])?.id ?? null;
      }
      return { ...state, entries, selectedId };
    }
    case 'select':
      return state.entries.some((entry) => entry.id === action.id) ? { ...state, selectedId: action.id } : state;
    case 'exported':
      return update(state, action.id, (entry) => (entry.exported ? entry : { ...entry, exported: true }));
    case 'zipFetched':
      return update(state, action.id, (entry) => ({ ...entry, zipMark: state.zipCount }));
    case 'zipMissing':
      return update(state, action.id, (entry) => (entry.zipGone ? entry : { ...entry, zipGone: true }));
    default:
      return state;
  }
}

export function selectedEntry(state: ExtractState): ExtractEntry | null {
  return state.entries.find((entry) => entry.id === state.selectedId) ?? null;
}

/** The next file to read, in list order. */
export function nextQueued(state: ExtractState): ExtractEntry | null {
  return state.entries.find((entry) => entry.status === 'queued') ?? null;
}

/** The short line under a file name in the list: "2쪽 · 표 1개", "읽는 중", … */
export function entryMeta(entry: ExtractEntry): string {
  switch (entry.status) {
    case 'queued':
      return listCopy.queued;
    case 'reading':
      return listCopy.reading;
    case 'done':
      return entry.result ? listMeta(entry.result) : '';
    case 'error':
      return entry.error?.kind === 'unsupportedFile' ? listCopy.unsupported : listCopy.failed;
    default:
      return '';
  }
}

/** Whether the server has surely dropped this result's ZIP: it said so, or 10 newer ZIPs exist. */
export function isZipExpired(state: ExtractState, entry: ExtractEntry): boolean {
  return entry.status === 'done' && (entry.zipGone || state.zipCount - entry.zipMark >= SERVER_KEEPS_ZIPS);
}

export interface StatusLine {
  message: string;
  tone: StatusTone;
  startedAt: number | null;
  endedAt: number | null;
}

/** What the status line says. It never uses the word "단계". */
export function statusFor(state: ExtractState): StatusLine {
  const { entries } = state;
  // A queued file is about to start, so it reads as busy too (never "다 읽었어요" too early).
  const active = entries.find((entry) => entry.status === 'reading') ?? nextQueued(state);
  if (active) {
    const position = entries.length > 1 ? { index: entries.indexOf(active) + 1, total: entries.length } : null;
    return {
      message: statusText.reading(active.name, position),
      tone: 'busy',
      startedAt: active.status === 'reading' ? active.startedAt : null,
      endedAt: null,
    };
  }
  if (entries.length === 0) {
    return { message: statusText.ready, tone: 'idle', startedAt: null, endedAt: null };
  }
  const done = entries.filter((entry) => entry.status === 'done');
  const failed = entries.length - done.length;
  if (entries.length === 1) {
    const only = entries[0] as ExtractEntry;
    if (only.status === 'done' && only.result) {
      return {
        message: statusText.doneOne(only.name, only.result.tables.length, only.result.visuals.length),
        tone: 'done',
        startedAt: only.startedAt,
        endedAt: only.endedAt,
      };
    }
    return { message: statusText.failedOne(only.name), tone: 'error', startedAt: null, endedAt: null };
  }
  if (failed === 0) return { message: statusText.doneAll(entries.length), tone: 'done', startedAt: null, endedAt: null };
  if (done.length === 0) return { message: statusText.failedAll(entries.length), tone: 'error', startedAt: null, endedAt: null };
  return {
    message: statusText.doneSome(entries.length, done.length, failed),
    tone: 'warning',
    startedAt: null,
    endedAt: null,
  };
}

/** 1 넣기 until a result exists, 2 확인·다듬기, 3 내보내기 once the shown result was copied or saved. */
export function stepFor(state: ExtractState): StepNumber {
  const selected = selectedEntry(state);
  if (selected?.status === 'done' && selected.exported) return 3;
  return state.entries.some((entry) => entry.status === 'done') ? 2 : 1;
}
