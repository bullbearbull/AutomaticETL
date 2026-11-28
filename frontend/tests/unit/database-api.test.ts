import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  downloadFileName,
  fetchDownloadFile,
  normalizeAnalyzeResponse,
  normalizeLoadDbResponse,
} from '../../src/features/database/api.ts';
import { cardinalityLabel } from '../../src/features/database/copy.ts';
import { AppError } from '../../src/lib/errors.ts';

test('normalizeAnalyzeResponse parses valid analyze payload defensively', () => {
  const parsed = normalizeAnalyzeResponse({
    session_id: 'sess-123',
    bundle_id: 'bundle-456',
    elapsed_seconds: 1.23,
    purpose: 'oltp',
    selected_candidate_id: 'C1_RELATIONAL',
    tables: [
      {
        source: {
          table_id: 't0',
          table_name: 'customers',
          display_name: 'customers.csv',
          source_path: 'customers.csv',
          source_kind: 'csv',
          sheet_name: null,
          row_count: 10,
          column_count: 3,
        },
        ingest: { files: [{ encoding: 'utf-8' }] },
        profiles: [
          {
            column: 'customer_name',
            count: 10,
            missing_count: 0,
            missing_rate: 0,
            unique_count: 10,
            top_patterns: [{ mask: '가가가', count: 10, rate: 1 }],
          },
        ],
        classifications: [
          {
            column: 'customer_name',
            physical_type: 'string',
            semantic_type: 'free_text',
            category: 'free_text',
            missing_count: 0,
            missing_rate: 0,
            unique_count: 10,
            reasons: ['text'],
          },
        ],
        warnings: [],
      },
    ],
    relations: {
      primary_keys: {
        customers: {
          table_name: 'customers',
          columns: ['customer_id'],
          is_composite: false,
          recommend_surrogate: false,
          surrogate_column_name: null,
          referenced_by_count: 1,
          reasons: ['unique 100%'],
        },
      },
      foreign_keys: [],
    },
    load_plan: {
      selected_candidate_id: 'C1_RELATIONAL',
      scorecards: [
        {
          candidate_id: 'C1_RELATIONAL',
          weighted_total: 88.5,
          integrity: { score: 95 },
          redundancy: { score: 85 },
          fitness: { score: 90 },
          validation: {
            trial_load_executed: true,
            trial_load_ok: true,
            constraint_violation_count: 0,
            cast_failure_count: 0,
          },
        },
      ],
      load_order: [{ table: 'customers', depends_on: [] }],
      table_plans: [
        {
          source_table: 'customers',
          target_table: 'customers',
          source_encoding: 'utf-8',
          load_order_index: 0,
          depends_on: [],
          primary_key: ['customer_id'],
          uses_surrogate_key: false,
          increment_mode: 'upsert',
          column_mappings: [
            {
              source_column: 'customer_name',
              target_table: 'customers',
              target_column: 'customer_name',
              transform: 'identity',
              type_cast: 'varchar(30)',
              null_handling: 'reject_row',
            },
          ],
          sensitive_columns: ['customer_name'],
          column_masking: { customer_name: 'none' },
          confirmed_unique_columns: [],
          expected_row_count: 10,
          notes: ['민감(PII) 컬럼 `customer_name` [L2 / name]: 마스킹'],
        },
      ],
      ddl_sql: 'CREATE TABLE customers (...);',
      erd_mermaid: 'erDiagram',
    },
    analysis_report_markdown: '# Report',
    warnings: [],
  });

  assert.equal(parsed.sessionId, 'sess-123');
  assert.equal(parsed.bundleId, 'bundle-456');
  assert.equal(parsed.tables.length, 1);
  assert.equal(parsed.tables[0]?.source.tableName, 'customers');
  assert.equal(parsed.loadPlan.tablePlans[0]?.sensitiveColumns[0], 'customer_name');
  assert.equal(Object.getPrototypeOf(parsed.loadPlan.tablePlans[0]?.columnMasking), null);
  assert.equal(parsed.loadPlan.tablePlans[0]?.columnMasking['constructor'], undefined);
  assert.equal(parsed.loadPlan.scorecards[0]?.trialLoadOk, true);
});

test('normalizeAnalyzeResponse throws AppError when session_id or tables missing', () => {
  assert.throws(
    () => normalizeAnalyzeResponse({ session_id: 'abc', tables: [] }),
    (err: unknown) => err instanceof AppError && err.kind === 'unreadable',
  );
  assert.throws(
    () => normalizeAnalyzeResponse({ tables: [{ source: { table_name: 'a' } }] }),
    (err: unknown) => err instanceof AppError && err.kind === 'server',
  );
});

test('normalizeLoadDbResponse handles load outcome and quarantine rows', () => {
  const res = normalizeLoadDbResponse({
    download_token: 'tok-1',
    download_filename: 'autoetl_data.sqlite',
    load_result: {
      batch_id: 'b-1',
      loaded_at: '2026-11-28T01:00:00Z',
      db_engine: 'sqlite',
      status: 'partial',
      tables: [
        {
          target_table: 'customers',
          dataset_kind: 'master_reference',
          source_name: 'customers.csv',
          rows_read: 5,
          rows_loaded: 4,
          rows_quarantined: 1,
          masked_columns: ['customer_name'],
          skipped_duplicate_file: false,
        },
      ],
      total_rows_read: 5,
      total_rows_loaded: 4,
      total_rows_quarantined: 1,
      warnings: [],
    },
    catalog: {
      quarantine: [
        {
          target_table: 'customers',
          source_row_number: 3,
          error_category: 'fk_orphan',
          error_column: 'branch_code',
          error_detail: 'missing parent key',
        },
      ],
    },
    management_ddl_sql: '',
  });

  assert.equal(res.downloadToken, 'tok-1');
  assert.equal(res.loadResult.status, 'partial');
  assert.equal(res.quarantine.length, 1);
  assert.equal(res.quarantine[0]?.sourceRowNumber, 3);
});

test('normalizeLoadDbResponse rejects missing or unknown load status and cardinalityLabel distinguishes N:1', () => {
  assert.throws(
    () =>
      normalizeLoadDbResponse({
        download_filename: 'autoetl_data.sqlite',
        load_result: { batch_id: 'b1', db_engine: 'sqlite', status: 'mystery' },
      }),
    (err: unknown) => err instanceof AppError && err.kind === 'server',
  );
  assert.throws(
    () =>
      normalizeLoadDbResponse({
        download_filename: 'autoetl_data.sqlite',
        load_result: { batch_id: 'b1', db_engine: 'sqlite' },
      }),
    (err: unknown) => err instanceof AppError && err.kind === 'server',
  );
  assert.equal(cardinalityLabel('N:1'), '다대일(N:1)');
  assert.equal(cardinalityLabel('1:N'), '일대다(1:N)');
});

test('downloadFileName and fetchDownloadFile parse Content-Disposition and handle statuses', async () => {
  assert.equal(
    downloadFileName("attachment; filename*=UTF-8''%EA%B2%B0%EA%B3%BC.sqlite", 'fallback.sqlite'),
    '결과.sqlite',
  );
  assert.equal(downloadFileName('attachment; filename="data.duckdb"', 'fallback.duckdb'), 'data.duckdb');
  assert.equal(downloadFileName(null, 'autoetl_data.sqlite'), 'autoetl_data.sqlite');

  const ok = await fetchDownloadFile('tok', 'fallback.sqlite', new AbortController().signal, {
    fetch: async () =>
      new Response(new Uint8Array([1, 2, 3]), {
        status: 200,
        headers: { 'Content-Disposition': 'attachment; filename="out.sqlite"' },
      }),
  });
  assert.equal(ok.type, 'ok');
  if (ok.type === 'ok') assert.equal(ok.fileName, 'out.sqlite');

  const missing = await fetchDownloadFile('tok', 'fallback.sqlite', new AbortController().signal, {
    fetch: async () => new Response('gone', { status: 404 }),
  });
  assert.deepEqual(missing, { type: 'missing' });

  const failed = await fetchDownloadFile('tok', 'fallback.sqlite', new AbortController().signal, {
    fetch: async () => new Response('err', { status: 500 }),
  });
  assert.deepEqual(failed, { type: 'failed', status: 500 });
});
