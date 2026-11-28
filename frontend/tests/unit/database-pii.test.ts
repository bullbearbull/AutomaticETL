import assert from 'node:assert/strict';
import { test } from 'node:test';
import { normalizeAnalyzeResponse } from '../../src/features/database/api.ts';
import { evidenceCopy, levelLabel } from '../../src/features/database/copy.ts';
import { setOverrideDraft } from '../../src/features/database/overrides.ts';
import {
  extractPiiColumns,
  formatPiiPreviewList,
  groupPiiByTable,
  unmaskedPiiColumns,
} from '../../src/features/database/pii.ts';

function sampleAnalysis() {
  return normalizeAnalyzeResponse({
    session_id: 's1',
    bundle_id: 'b1',
    purpose: 'oltp',
    tables: [
      {
        source: {
          table_id: 't0',
          table_name: 'customers',
          display_name: 'customers.csv',
          source_path: 'customers.csv',
          source_kind: 'csv',
          row_count: 5,
          column_count: 3,
        },
        profiles: [
          {
            column: 'customer_name',
            count: 5,
            missing_count: 0,
            missing_rate: 0,
            unique_count: 5,
            top_patterns: [{ mask: '가가가', count: 5, rate: 1 }],
          },
          {
            column: 'phone',
            count: 5,
            missing_count: 0,
            missing_rate: 0,
            unique_count: 5,
            top_patterns: [{ mask: '999-9999-9999', count: 5, rate: 1 }],
          },
        ],
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
            { source_column: 'phone', target_table: 'customers', target_column: 'phone', type_cast: 'varchar', null_handling: 'null' },
          ],
          sensitive_columns: ['customer_name', 'phone'],
          column_masking: { customer_name: 'mask', phone: 'none' },
          expected_row_count: 5,
          notes: [
            '민감(PII) 컬럼 `customer_name` [L2 / name]: 가운데 글자 마스킹',
            '민감(PII) 컬럼 `phone` [L2 / phone_number]: 가운데 4자리 마스킹',
          ],
        },
      ],
    },
  });
}

test('extractPiiColumns parses notes, pattern masks, and effective overrides', () => {
  const analysis = sampleAnalysis();
  const initial = extractPiiColumns(analysis, {});
  assert.equal(initial.length, 2);
  assert.equal(initial[0]?.typeLabels[0], '이름');
  assert.equal(initial[0]?.patternMask, '가가가');
  assert.equal(initial[0]?.effectiveMasking, 'mask');
  assert.equal(initial[1]?.typeLabels[0], '전화번호');
  assert.equal(initial[1]?.effectiveMasking, 'none');

  const unmasked = unmaskedPiiColumns(analysis, {});
  assert.deepEqual(unmasked.map((u) => u.sourceColumn), ['phone']);

  // User overrides phone to 'hash' -> zero unmasked columns left.
  const drafts = setOverrideDraft({}, 'customers', 'phone', { masking: 'hash' });
  assert.equal(unmaskedPiiColumns(analysis, drafts).length, 0);
});

test('groupPiiByTable and formatPiiPreviewList summarize synthetic examples', () => {
  const analysis = sampleAnalysis();
  const all = extractPiiColumns(analysis, {});
  assert.deepEqual(groupPiiByTable(all), [{ table: 'customers', columns: ['customer_name', 'phone'] }]);
  assert.equal(formatPiiPreviewList(all), '김*수, 010-****-5678');
});

test('levelLabel treats L3 as highest sensitivity and L1 as lowest', () => {
  assert.equal(levelLabel('L3'), '고위험 식별정보');
  assert.equal(levelLabel('L2'), '개인정보');
  assert.equal(levelLabel('L1'), '민감 참고정보');
});

test('extractPiiColumns matches backend mask_value previews and builds factual reason', () => {
  const analysis = normalizeAnalyzeResponse({
    session_id: 's2',
    bundle_id: 'b2',
    purpose: 'oltp',
    tables: [
      {
        source: { table_id: 't0', table_name: 'kyc', display_name: 'kyc.csv', row_count: 1, column_count: 5 },
        profiles: [],
        classifications: [],
      },
    ],
    relations: { primary_keys: {}, foreign_keys: [] },
    load_plan: {
      table_plans: [
        {
          source_table: 'kyc',
          target_table: 'kyc',
          load_order_index: 0,
          column_mappings: [],
          sensitive_columns: ['rrn', 'acct', 'client_ip', 'biz_no', 'secret_note'],
          column_masking: {},
          expected_row_count: 1,
          notes: [
            '민감(PII) 컬럼 `rrn` [L3 / resident_registration_number]: 주민등록번호 뒷자리 마스킹',
            '민감(PII) 컬럼 `acct` [L2 / bank_account]: 계좌 마스킹',
            '민감(PII) 컬럼 `client_ip` [L1 / ip_address]: IP 마지막 옥텟 마스킹',
            '민감(PII) 컬럼 `biz_no` [L1 / business_registration_number]: 사업자번호 뒷자리 마스킹',
          ],
        },
      ],
    },
  });

  const byCol = new Map(extractPiiColumns(analysis, {}).map((item) => [item.sourceColumn, item]));
  assert.equal(byCol.get('rrn')?.maskExample, '900101-1******');
  assert.equal(byCol.get('acct')?.maskExample, '110-***-***789');
  assert.equal(byCol.get('client_ip')?.maskExample, '192.168.1.***');
  assert.equal(byCol.get('biz_no')?.maskExample, '123-**-***90');
  assert.equal(byCol.get('secret_note')?.maskExample, '일부 글자를 *로 가림');

  const rrnReason = evidenceCopy.piiReasonText(
    'rrn',
    byCol.get('rrn')?.typeLabels ?? [],
    byCol.get('rrn')?.level ?? null,
    null,
    byCol.get('rrn')?.recommendation,
  );
  assert.match(rrnReason, /고위험 식별정보/);
  assert.match(rrnReason, /주민등록번호 뒷자리 마스킹/);
  assert.doesNotMatch(rrnReason, /값 모양/);
});
