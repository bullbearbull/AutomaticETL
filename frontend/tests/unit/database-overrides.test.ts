import assert from 'node:assert/strict';
import { test } from 'node:test';
import { normalizeAnalyzeResponse } from '../../src/features/database/api.ts';
import {
  applyMaskToUnmaskedColumns,
  buildSchemaOverrideSpec,
  classifyTableMappings,
  countActiveOverrides,
  hasInvalidTargetColumnNames,
  isValidTargetColumnName,
  setOverrideDraft,
} from '../../src/features/database/overrides.ts';

test('identifier validation enforces ^[a-z][a-z0-9_]*$ or empty', () => {
  assert.equal(isValidTargetColumnName(''), true);
  assert.equal(isValidTargetColumnName('customer_full_name'), true);
  assert.equal(isValidTargetColumnName('col_2'), true);
  assert.equal(isValidTargetColumnName('2bad'), false);
  assert.equal(isValidTargetColumnName('UpperCase'), false);
  assert.equal(isValidTargetColumnName('한글열'), false);
  assert.equal(isValidTargetColumnName('has space'), false);
});

test('buildSchemaOverrideSpec emits valid overrides and filters unknown columns', () => {
  const analysis = normalizeAnalyzeResponse({
    session_id: 's1',
    tables: [{ source: { table_name: 'customers', display_name: 'customers.csv', row_count: 1, column_count: 2 } }],
    load_plan: {
      table_plans: [
        {
          source_table: 'customers',
          target_table: 'customers',
          load_order_index: 0,
          column_mappings: [
            { source_column: 'phone', target_table: 'customers', target_column: 'phone', type_cast: 'varchar', null_handling: 'null' },
          ],
          sensitive_columns: ['phone'],
          column_masking: {},
          expected_row_count: 1,
        },
      ],
    },
  });

  let drafts = setOverrideDraft({}, 'customers', 'phone', {
    targetColumn: 'mobile_no',
    unique: true,
    masking: 'mask',
  });
  drafts = setOverrideDraft(drafts, 'ghost_table', 'x', { masking: 'hash' });

  assert.equal(hasInvalidTargetColumnNames(drafts), false);
  assert.equal(countActiveOverrides(drafts), 2);

  const spec = buildSchemaOverrideSpec(
    drafts,
    { orphanPolicy: 'reject_all', includeAuditColumns: false, includeManagementTables: true },
    analysis,
  );
  assert.equal(spec.orphan_policy, 'reject_all');
  assert.equal(spec.include_audit_columns, false);
  assert.equal(spec.columns.length, 1);
  assert.deepEqual(spec.columns[0], {
    table: 'customers',
    source_column: 'phone',
    target_column: 'mobile_no',
    unique: true,
    masking: 'mask',
  });
});

test('applyMaskToUnmaskedColumns preserves existing target names while masking PII', () => {
  const drafts = setOverrideDraft({}, 'customers', 'customer_name', {
    targetColumn: 'member_name',
  });
  const masked = applyMaskToUnmaskedColumns(drafts, [
    { table: 'customers', sourceColumn: 'customer_name' },
    { table: 'customers', sourceColumn: 'phone' },
  ]);

  assert.equal(masked['customers\0customer_name']?.targetColumn, 'member_name');
  assert.equal(masked['customers\0customer_name']?.masking, 'mask');
  assert.equal(masked['customers\0phone']?.masking, 'mask');
});

test('classifyTableMappings isolates surrogate and decomposed rows from editable columns', () => {
  const classified = classifyTableMappings([
    {
      sourceColumn: '__surrogate__',
      targetTable: 'logs',
      targetColumn: 'logs_id',
      transform: 'generated_always_as_identity',
      typeCast: 'integer',
      nullHandling: 'reject_row',
    },
    {
      sourceColumn: 'item_code',
      targetTable: 'logs',
      targetColumn: 'item_code',
      transform: 'identity',
      typeCast: 'varchar',
      nullHandling: 'null',
    },
    {
      sourceColumn: 'item_code',
      targetTable: 'logs',
      targetColumn: 'item_code_year',
      transform: "NULLIF(regexp_extract(item_code, '^ORD-(\\d+)', 1), '')",
      typeCast: 'integer',
      nullHandling: 'null',
    },
  ]);

  assert.deepEqual(
    classified.map((c) => [c.mapping.targetColumn, c.kind]),
    [
      ['logs_id', 'surrogate'],
      ['item_code', 'editable'],
      ['item_code_year', 'decomposed'],
    ],
  );
});

test('hasInvalidTargetColumnNames scopes validation to editable columns in current analysis', () => {
  const analysis = normalizeAnalyzeResponse({
    session_id: 's1',
    tables: [{ source: { table_name: 'orders', display_name: 'orders.csv', row_count: 1, column_count: 1 } }],
    load_plan: {
      table_plans: [
        {
          source_table: 'orders',
          target_table: 'orders',
          load_order_index: 0,
          column_mappings: [
            { source_column: 'amount', target_table: 'orders', target_column: 'amount', transform: 'identity', type_cast: 'int', null_handling: 'null' },
          ],
        },
      ],
    },
  });

  const drafts = setOverrideDraft({}, 'removed_table', 'col_a', { targetColumn: '2bad' });
  assert.equal(hasInvalidTargetColumnNames(drafts), true);
  assert.equal(hasInvalidTargetColumnNames(drafts, analysis), false);
});
