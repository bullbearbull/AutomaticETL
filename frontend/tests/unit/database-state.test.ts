import assert from 'node:assert/strict';
import { test } from 'node:test';
import { normalizeAnalyzeResponse, normalizeLoadDbResponse } from '../../src/features/database/api.ts';
import { setOverrideDraft } from '../../src/features/database/overrides.ts';
import {
  canSaveDatabase,
  databaseReducer,
  DEFAULT_DATABASE_SETTINGS,
  initialDatabaseState,
  isAnalysisStale,
  statusFor,
  stepFor,
} from '../../src/features/database/state.ts';
import { AppError } from '../../src/lib/errors.ts';

function fakeFile(name: string, size = 100): File {
  return new File([new Uint8Array(size)], name, { type: 'text/csv' });
}

function sampleAnalyze() {
  return normalizeAnalyzeResponse({
    session_id: 's1',
    bundle_id: 'b1',
    elapsed_seconds: 0.4,
    purpose: 'oltp',
    tables: [
      {
        source: {
          table_id: 't0',
          table_name: 'customers',
          display_name: 'customers.csv',
          source_path: 'customers.csv',
          row_count: 2,
          column_count: 2,
        },
        profiles: [],
        classifications: [],
      },
    ],
    relations: { primary_keys: {}, foreign_keys: [] },
    load_plan: {
      table_plans: [
        {
          source_table: 'customers',
          target_table: 'customers',
          load_order_index: 0,
          column_mappings: [
            { source_column: 'customer_name', target_table: 'customers', target_column: 'customer_name', type_cast: 'varchar', null_handling: 'null' },
          ],
          sensitive_columns: ['customer_name'],
          column_masking: { customer_name: 'none' },
          expected_row_count: 2,
        },
      ],
    },
  });
}

test('default settings enable decomposeStrings matching backend engine defaults', () => {
  assert.equal(DEFAULT_DATABASE_SETTINGS.decomposeStrings, true);
});

test('step transitions require matching fileRevision and markExported after download', () => {
  let state = initialDatabaseState;
  assert.equal(stepFor(state), 1);
  assert.equal(canSaveDatabase(state), false);
  assert.equal(statusFor(state).tone, 'idle');
  assert.equal(statusFor(state).message.includes('단계'), false);

  state = databaseReducer(state, {
    type: 'mergeFiles',
    items: [{ id: 'f1', file: fakeFile('customers.csv'), name: 'customers.csv', size: 100 }],
  });
  state = databaseReducer(state, { type: 'startAnalyze', mode: 'initial', now: 1000 });
  assert.equal(statusFor(state).tone, 'busy');
  assert.equal(canSaveDatabase(state), false);

  state = databaseReducer(state, {
    type: 'analyzeSucceeded',
    result: sampleAnalyze(),
    fileRevision: state.fileRevision,
    now: 1500,
  });
  assert.equal(stepFor(state), 2);
  assert.equal(isAnalysisStale(state), false);
  assert.equal(canSaveDatabase(state), true);
  assert.equal(state.selectedTable, 'customers');
  assert.equal(statusFor(state).tone, 'warning');

  state = databaseReducer(state, { type: 'setSettings', patch: { purpose: 'archive' } });
  assert.equal(statusFor(state).tone, 'done');

  const loadOutcome = normalizeLoadDbResponse({
    download_token: 'tok-1',
    download_filename: 'autoetl_data.sqlite',
    load_result: {
      batch_id: 'b1',
      loaded_at: '2026-11-28T01:00:00Z',
      db_engine: 'sqlite',
      status: 'succeeded',
      tables: [],
      total_rows_read: 2,
      total_rows_loaded: 2,
      total_rows_quarantined: 0,
    },
  });
  // saveDbSucceeded alone does not mark exported until download completes via markExported.
  state = databaseReducer(state, {
    type: 'saveDbSucceeded',
    outcome: loadOutcome,
    fileRevision: state.fileRevision,
    editRevision: state.editRevision,
    now: 2000,
  });
  assert.equal(stepFor(state), 2);

  // Stale markExported from an older editRevision is ignored.
  state = databaseReducer(state, {
    type: 'markExported',
    fileRevision: state.fileRevision,
    editRevision: state.editRevision - 1,
  });
  assert.equal(stepFor(state), 2);

  state = databaseReducer(state, {
    type: 'markExported',
    fileRevision: state.fileRevision,
    editRevision: state.editRevision,
  });
  assert.equal(stepFor(state), 3);

  state = databaseReducer(state, { type: 'removeFile', id: 'f1' });
  assert.equal(stepFor(state), 1);
  assert.equal(state.analysis, null);
  assert.equal(state.settings.purpose, 'archive');
});

test('adding or removing a file marks previous analysis stale and blocks saving even on refresh failure', () => {
  let state = databaseReducer(initialDatabaseState, {
    type: 'mergeFiles',
    items: [{ id: 'f1', file: fakeFile('customers.csv'), name: 'customers.csv', size: 100 }],
  });
  state = databaseReducer(state, {
    type: 'analyzeSucceeded',
    result: sampleAnalyze(),
    fileRevision: state.fileRevision,
    now: 100,
  });
  state = databaseReducer(state, {
    type: 'setOverrides',
    overrides: setOverrideDraft({}, 'customers', 'customer_name', { masking: 'mask' }),
  });
  assert.equal(canSaveDatabase(state), true);

  // Add second file -> previous analysis is marked stale, save is blocked immediately.
  state = databaseReducer(state, {
    type: 'mergeFiles',
    items: [{ id: 'f2', file: fakeFile('orders.csv'), name: 'orders.csv', size: 80 }],
  });
  assert.equal(isAnalysisStale(state), true);
  assert.equal(canSaveDatabase(state), false);

  // If refresh fails, old analysis stays visible for reference, but save stays disabled.
  const err = new AppError({ kind: 'server', what: 'fail', why: 'why', how: 'how' });
  state = databaseReducer(state, { type: 'analyzeFailed', error: err, now: 200 });
  assert.equal(isAnalysisStale(state), true);
  assert.equal(canSaveDatabase(state), false);
  assert.equal(statusFor(state).tone, 'error');

  // Remove the first file -> prunes drafts tied to customers.csv and keeps remaining file stale until re-analyzed.
  state = databaseReducer(state, { type: 'removeFile', id: 'f1' });
  assert.deepEqual(state.overrides, {});
  assert.equal(canSaveDatabase(state), false);
});

test('replacing a file with the same name updates it in place instead of duplicating', () => {
  let state = databaseReducer(initialDatabaseState, {
    type: 'mergeFiles',
    items: [{ id: 'f1', file: fakeFile('a.csv', 10), name: 'a.csv', size: 10 }],
  });
  state = databaseReducer(state, {
    type: 'mergeFiles',
    items: [
      { id: 'f2', file: fakeFile('a.csv', 20), name: 'a.csv', size: 20 },
      { id: 'f3', file: fakeFile('b.csv', 30), name: 'b.csv', size: 30 },
    ],
  });
  assert.equal(state.files.length, 2);
  assert.equal(state.files[0]?.id, 'f2');
  assert.equal(state.files[1]?.name, 'b.csv');
});

test('analyzeSucceeded ignores responses from superseded fileRevision and failed load sets error status', () => {
  let state = databaseReducer(initialDatabaseState, {
    type: 'mergeFiles',
    items: [{ id: 'f1', file: fakeFile('a.csv'), name: 'a.csv', size: 10 }],
  });
  const oldRev = state.fileRevision;
  state = databaseReducer(state, {
    type: 'mergeFiles',
    items: [{ id: 'f2', file: fakeFile('b.csv'), name: 'b.csv', size: 10 }],
  });
  state = databaseReducer(state, {
    type: 'analyzeSucceeded',
    result: sampleAnalyze(),
    fileRevision: oldRev,
    now: 50,
  });
  assert.equal(state.analysis, null);

  state = databaseReducer(state, {
    type: 'analyzeSucceeded',
    result: sampleAnalyze(),
    fileRevision: state.fileRevision,
    now: 60,
  });
  const failedOutcome = normalizeLoadDbResponse({
    download_filename: 'autoetl_data.sqlite',
    load_result: {
      batch_id: 'b2',
      loaded_at: '2026-11-28T01:00:00Z',
      db_engine: 'sqlite',
      status: 'failed',
      tables: [],
      total_rows_read: 2,
      total_rows_loaded: 0,
      total_rows_quarantined: 1,
      warnings: ['오류 행 발생으로 전체 중단됨'],
    },
  });
  state = databaseReducer(state, {
    type: 'saveDbSucceeded',
    outcome: failedOutcome,
    fileRevision: state.fileRevision,
    editRevision: state.editRevision,
    now: 70,
  });
  assert.equal(stepFor(state), 2);
  assert.equal(statusFor(state).tone, 'error');
});

test('savingDb always clears when revisions drift during save or save fails/cancels', () => {
  let base = databaseReducer(initialDatabaseState, {
    type: 'mergeFiles',
    items: [{ id: 'f1', file: fakeFile('customers.csv'), name: 'customers.csv', size: 100 }],
  });
  base = databaseReducer(base, {
    type: 'analyzeSucceeded',
    result: sampleAnalyze(),
    fileRevision: base.fileRevision,
    now: 50,
  });

  const outcome = normalizeLoadDbResponse({
    download_token: 'tok-drift',
    download_filename: 'autoetl_data.sqlite',
    load_result: {
      batch_id: 'b3',
      loaded_at: '2026-11-28T01:00:00Z',
      db_engine: 'sqlite',
      status: 'succeeded',
      tables: [],
      total_rows_read: 2,
      total_rows_loaded: 2,
      total_rows_quarantined: 0,
    },
  });

  // Drift before saveDbSucceeded: savingDb must clear to false without publishing stale outcome.
  let state = databaseReducer(base, { type: 'startSaveDb', now: 100 });
  const startEditRev = state.editRevision;
  assert.equal(state.savingDb, true);
  state = databaseReducer(state, { type: 'setSettings', patch: { notes: 'changed' } });
  state = databaseReducer(state, {
    type: 'saveDbSucceeded',
    outcome,
    fileRevision: state.fileRevision,
    editRevision: startEditRev,
    now: 200,
  });
  assert.equal(state.savingDb, false);
  assert.equal(state.loadOutcome, null);
  assert.equal(stepFor(state), 2);

  // Drift before saveDbFailed: savingDb must clear without surfacing a stale noticeError.
  const err = new AppError({ kind: 'server', what: 'fail', why: 'why', how: 'how' });
  state = databaseReducer(base, { type: 'startSaveDb', now: 300 });
  const failEditRev = state.editRevision;
  state = databaseReducer(state, { type: 'setSettings', patch: { notes: 'again' } });
  state = databaseReducer(state, {
    type: 'saveDbFailed',
    error: err,
    fileRevision: state.fileRevision,
    editRevision: failEditRev,
    now: 400,
  });
  assert.equal(state.savingDb, false);
  assert.equal(state.noticeError, null);

  // cancelSaveDb clears savingDb cleanly.
  state = databaseReducer(base, { type: 'startSaveDb', now: 500 });
  assert.equal(state.savingDb, true);
  state = databaseReducer(state, { type: 'cancelSaveDb', now: 600 });
  assert.equal(state.savingDb, false);

  // Matching revision failure clears savingDb and sets noticeError.
  state = databaseReducer(base, { type: 'startSaveDb', now: 700 });
  state = databaseReducer(state, {
    type: 'saveDbFailed',
    error: err,
    fileRevision: state.fileRevision,
    editRevision: state.editRevision,
    now: 800,
  });
  assert.equal(state.savingDb, false);
  assert.equal(state.noticeError, err);
});
