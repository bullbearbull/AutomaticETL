import assert from 'node:assert/strict';
import { test } from 'node:test';
import { normalizeAnalyzeResponse, normalizeLoadDbResponse } from '../../src/features/database/api.ts';
import {
  databaseReducer,
  initialDatabaseState,
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

test('step transitions cleanly from 1 to 2 to 3 and resets on clear/remove', () => {
  let state = initialDatabaseState;
  assert.equal(stepFor(state), 1);
  assert.equal(statusFor(state).tone, 'idle');
  assert.equal(statusFor(state).message.includes('단계'), false);

  state = databaseReducer(state, {
    type: 'mergeFiles',
    items: [{ id: 'f1', file: fakeFile('customers.csv'), name: 'customers.csv', size: 100 }],
  });
  state = databaseReducer(state, { type: 'startAnalyze', mode: 'initial', now: 1000 });
  assert.equal(statusFor(state).tone, 'busy');
  assert.equal(statusFor(state).message.includes('단계'), false);

  state = databaseReducer(state, { type: 'analyzeSucceeded', result: sampleAnalyze(), now: 1500 });
  assert.equal(stepFor(state), 2);
  assert.equal(state.selectedTable, 'customers');
  // Unmasked PII column emits warning tone in status bar unless archive purpose is selected.
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
  state = databaseReducer(state, { type: 'saveDbSucceeded', outcome: loadOutcome, now: 2000 });
  assert.equal(stepFor(state), 3);
  assert.equal(statusFor(state).tone, 'done');

  state = databaseReducer(state, { type: 'removeFile', id: 'f1' });
  assert.equal(stepFor(state), 1);
  assert.equal(state.analysis, null);
  assert.equal(state.settings.purpose, 'archive');
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

test('analyzeFailed sets blocking error when no analysis exists, or noticeError when analysis exists', () => {
  const err = new AppError({ kind: 'server', what: 'fail', why: 'why', how: 'how' });
  const blocked = databaseReducer(initialDatabaseState, { type: 'analyzeFailed', error: err, now: 10 });
  assert.equal(blocked.error, err);
  assert.equal(blocked.noticeError, null);

  const ready = databaseReducer(initialDatabaseState, {
    type: 'analyzeSucceeded',
    result: sampleAnalyze(),
    now: 20,
  });
  const noticed = databaseReducer(ready, { type: 'analyzeFailed', error: err, now: 30 });
  assert.equal(noticed.error, null);
  assert.equal(noticed.noticeError, err);
  assert.notEqual(noticed.analysis, null);
});
