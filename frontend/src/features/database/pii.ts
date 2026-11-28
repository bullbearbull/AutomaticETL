import type { AnalyzeResult, MaskingPolicy } from './api.ts';
import type { ColumnOverrideDraft } from './overrides.ts';
import { getOverrideDraft } from './overrides.ts';

export interface PiiColumnInfo {
  table: string;
  targetTable: string;
  sourceColumn: string;
  targetColumn: string;
  level: 'L1' | 'L2' | 'L3' | null;
  types: string[];
  typeLabels: string[];
  recommendation: string;
  patternMask: string | null;
  effectiveMasking: MaskingPolicy;
  maskExample: string;
  hashExample: string;
}

const PII_NOTE_RE = /^민감\(PII\)\s*컬럼\s*`([^`]+)`\s*\[(L[123])\s*\/\s*([^\]]+)\]:\s*(.*)$/;

export const SENSITIVE_TYPE_LABELS: Record<string, string> = {
  name: '이름',
  phone_number: '전화번호',
  email: '이메일',
  resident_registration_number: '주민등록번호',
  foreigner_registration_number: '외국인등록번호',
  card_number: '카드번호',
  bank_account: '계좌번호',
  date_of_birth: '생년월일',
  address: '주소',
  business_registration_number: '사업자등록번호',
  ip_address: 'IP 주소',
};

const SENSITIVE_TYPE_PREVIEWS: Record<string, string> = {
  name: '김*수',
  phone_number: '010-****-5678',
  email: 'mi***@example.com',
  resident_registration_number: '900101-*******',
  foreigner_registration_number: '900101-*******',
  card_number: '1234-****-****-5678',
  bank_account: '110-***-******',
  date_of_birth: '1990-**-**',
  address: '서울시 강남구 ***',
  business_registration_number: '123-**-*****',
  ip_address: '192.168.*.*',
};

const HASH_PREVIEW_EXAMPLE = 'h_a1b2c3d4… (일방향 변환)';

function labelForSensitiveType(rawType: string): string {
  const key = rawType.trim();
  return SENSITIVE_TYPE_LABELS[key] ?? '개인정보';
}

function maskPreviewForTypes(types: readonly string[], columnName: string): string {
  for (const t of types) {
    const preview = SENSITIVE_TYPE_PREVIEWS[t.trim()];
    if (preview) return preview;
  }
  const lower = columnName.toLowerCase();
  if (lower.includes('phone') || lower.includes('tel') || lower.includes('연락') || lower.includes('전화')) {
    return SENSITIVE_TYPE_PREVIEWS.phone_number ?? '***-****-****';
  }
  if (lower.includes('email') || lower.includes('mail') || lower.includes('이메일')) {
    return SENSITIVE_TYPE_PREVIEWS.email ?? 'ab***@example.com';
  }
  if (lower.includes('name') || lower.includes('성명') || lower.includes('이름')) {
    return SENSITIVE_TYPE_PREVIEWS.name ?? '김*수';
  }
  return '홍*동 / 010-****-1234';
}

function guessTypesFromColumnName(columnName: string): string[] {
  const lower = columnName.toLowerCase();
  if (lower.includes('phone') || lower.includes('tel') || lower.includes('mobile') || lower.includes('전화') || lower.includes('연락')) {
    return ['phone_number'];
  }
  if (lower.includes('email') || lower.includes('mail') || lower.includes('이메일')) {
    return ['email'];
  }
  if (lower.includes('name') || lower.includes('성명') || lower.includes('이름')) {
    return ['name'];
  }
  if (lower.includes('addr') || lower.includes('주소')) {
    return ['address'];
  }
  if (lower.includes('birth') || lower.includes('생년')) {
    return ['date_of_birth'];
  }
  if (lower.includes('card') || lower.includes('카드')) {
    return ['card_number'];
  }
  if (lower.includes('account') || lower.includes('계좌')) {
    return ['bank_account'];
  }
  return [];
}

/**
 * Extracts structured PII detections across all analyzed tables by combining
 * `loadPlan.tablePlans[].sensitiveColumns`, `notes`, `columnMasking`, `profiles`,
 * and any unsaved local `overrides`.
 */
export function extractPiiColumns(
  analysis: AnalyzeResult | null,
  overrides: Readonly<Record<string, ColumnOverrideDraft>> = {},
): PiiColumnInfo[] {
  if (!analysis) return [];
  const results: PiiColumnInfo[] = [];
  const profileByTable = new Map(analysis.tables.map((t) => [t.source.tableName, t.profiles]));

  for (const plan of analysis.loadPlan.tablePlans) {
    const parsedNotes = new Map<string, { level: 'L1' | 'L2' | 'L3'; types: string[]; recommendation: string }>();
    for (const note of plan.notes) {
      const match = PII_NOTE_RE.exec(note.trim());
      if (!match) continue;
      const col = match[1]?.trim() ?? '';
      const level = match[2] as 'L1' | 'L2' | 'L3';
      const rawTypes = (match[3] ?? '')
        .split(',')
        .map((part) => part.trim())
        .filter(Boolean);
      const recommendation = (match[4] ?? '').trim();
      if (col) parsedNotes.set(col, { level, types: rawTypes, recommendation });
    }

    const colSet = new Set<string>(plan.sensitiveColumns);
    for (const col of parsedNotes.keys()) colSet.add(col);

    const profiles = profileByTable.get(plan.sourceTable) ?? [];
    const profileMap = new Map(profiles.map((p) => [p.column, p]));
    const mappingMap = new Map(plan.columnMappings.map((m) => [m.sourceColumn, m]));

    for (const sourceColumn of colSet) {
      const noteInfo = parsedNotes.get(sourceColumn);
      const types = noteInfo && noteInfo.types.length > 0 ? noteInfo.types : guessTypesFromColumnName(sourceColumn);
      const typeLabels = types.length > 0 ? types.map(labelForSensitiveType) : ['개인정보'];
      const draft = getOverrideDraft(overrides, plan.sourceTable, sourceColumn);
      const serverPolicy = plan.columnMasking[sourceColumn] ?? 'none';
      const effectiveMasking: MaskingPolicy = draft.masking ?? serverPolicy;
      const profile = profileMap.get(sourceColumn);
      const mapping = mappingMap.get(sourceColumn);

      results.push({
        table: plan.sourceTable,
        targetTable: plan.targetTable,
        sourceColumn,
        targetColumn: mapping?.targetColumn || sourceColumn,
        level: noteInfo?.level ?? null,
        types,
        typeLabels,
        recommendation: noteInfo?.recommendation ?? '',
        patternMask: profile?.topPatterns[0]?.mask ?? null,
        effectiveMasking,
        maskExample: maskPreviewForTypes(types, sourceColumn),
        hashExample: HASH_PREVIEW_EXAMPLE,
      });
    }
  }

  return results;
}

/** Returns all detected PII columns whose effective policy is still `'none'` ("그대로"). */
export function unmaskedPiiColumns(
  analysis: AnalyzeResult | null,
  overrides: Readonly<Record<string, ColumnOverrideDraft>> = {},
): PiiColumnInfo[] {
  return extractPiiColumns(analysis, overrides).filter((item) => item.effectiveMasking === 'none');
}

/** Groups PII items by source table name for display in modals and banners. */
export function groupPiiByTable(items: readonly PiiColumnInfo[]): Array<{ table: string; columns: string[] }> {
  const map = new Map<string, string[]>();
  for (const item of items) {
    const existing = map.get(item.table);
    if (existing) {
      existing.push(item.sourceColumn);
    } else {
      map.set(item.table, [item.sourceColumn]);
    }
  }
  return Array.from(map.entries()).map(([table, columns]) => ({ table, columns }));
}

/** Builds up to 3 distinct synthetic masked examples (e.g. `김*수, 010-****-5678`). */
export function formatPiiPreviewList(items: readonly PiiColumnInfo[], limit = 3): string {
  const seen = new Set<string>();
  const examples: string[] = [];
  for (const item of items) {
    if (seen.has(item.maskExample)) continue;
    seen.add(item.maskExample);
    examples.push(item.maskExample);
    if (examples.length >= limit) break;
  }
  return examples.join(', ');
}
