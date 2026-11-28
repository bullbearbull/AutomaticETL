import type { AnalyzeResult, ColumnOverrideSpec, MaskingPolicy, SchemaOverrideSpec } from './api.ts';
import type { DatabaseSettings } from './state.ts';

/**
 * Matches the backend `ColumnOverride.target_column` Pydantic constraint
 * (`^[a-z][a-z0-9_]*$`).
 */
export const IDENTIFIER_PATTERN = /^[a-z][a-z0-9_]*$/;

export interface ColumnOverrideDraft {
  /** Edited DB target column name, or undefined if untouched. */
  targetColumn?: string | undefined;
  /** Explicit UNIQUE constraint override, or undefined if untouched. */
  unique?: boolean | undefined;
  /** PII protection policy override, or undefined if untouched. */
  masking?: MaskingPolicy | undefined;
}

export function overrideKey(table: string, sourceColumn: string): string {
  return `${table}\0${sourceColumn}`;
}

export function getOverrideDraft(
  drafts: Readonly<Record<string, ColumnOverrideDraft>>,
  table: string,
  sourceColumn: string,
): ColumnOverrideDraft {
  return drafts[overrideKey(table, sourceColumn)] ?? {};
}

/** Empty string means "keep automatic name"; non-empty must match `IDENTIFIER_PATTERN`. */
export function isValidTargetColumnName(value: string): boolean {
  const trimmed = value.trim();
  if (trimmed === '') return true;
  return IDENTIFIER_PATTERN.test(trimmed);
}

/** True when any edited DB column name violates the server identifier rule. */
export function hasInvalidTargetColumnNames(drafts: Readonly<Record<string, ColumnOverrideDraft>>): boolean {
  return Object.values(drafts).some(
    (draft) => draft.targetColumn !== undefined && !isValidTargetColumnName(draft.targetColumn),
  );
}

/** Returns a new draft map with the specified field updated; removes empty drafts. */
export function setOverrideDraft(
  drafts: Readonly<Record<string, ColumnOverrideDraft>>,
  table: string,
  sourceColumn: string,
  patch: Partial<ColumnOverrideDraft>,
): Record<string, ColumnOverrideDraft> {
  const key = overrideKey(table, sourceColumn);
  const current = drafts[key] ?? {};
  const next: ColumnOverrideDraft = { ...current, ...patch };
  if (next.targetColumn === undefined && next.unique === undefined && next.masking === undefined) {
    const copy = { ...drafts };
    delete copy[key];
    return copy;
  }
  return { ...drafts, [key]: next };
}

/** Counts how many columns have at least one user override set. */
export function countActiveOverrides(drafts: Readonly<Record<string, ColumnOverrideDraft>>): number {
  return Object.values(drafts).filter(
    (draft) =>
      (draft.targetColumn !== undefined && draft.targetColumn.trim() !== '') ||
      draft.unique !== undefined ||
      draft.masking !== undefined,
  ).length;
}

/**
 * Sets `masking = 'mask'` for every unmasked PII column while preserving
 * existing target column names and unique toggles. Used by `[가리고 저장하기]`.
 */
export function applyMaskToUnmaskedColumns(
  drafts: Readonly<Record<string, ColumnOverrideDraft>>,
  unmasked: ReadonlyArray<{ table: string; sourceColumn: string }>,
): Record<string, ColumnOverrideDraft> {
  let next: Record<string, ColumnOverrideDraft> = { ...drafts };
  for (const item of unmasked) {
    next = setOverrideDraft(next, item.table, item.sourceColumn, { masking: 'mask' });
  }
  return next;
}

/**
 * Builds the `SchemaOverrideSpec` payload sent to `/api/analyze-tables` and
 * `/api/etl/load-db`. Drops overrides that reference tables absent from the
 * current analysis or invalid column names (when analyzing partial drafts).
 */
export function buildSchemaOverrideSpec(
  drafts: Readonly<Record<string, ColumnOverrideDraft>>,
  settings: Pick<DatabaseSettings, 'orphanPolicy' | 'includeAuditColumns' | 'includeManagementTables'>,
  analysis: AnalyzeResult | null,
): SchemaOverrideSpec {
  const validColumnsByTable = new Map<string, Set<string>>();
  if (analysis) {
    for (const plan of analysis.loadPlan.tablePlans) {
      validColumnsByTable.set(
        plan.sourceTable,
        new Set(plan.columnMappings.map((m) => m.sourceColumn)),
      );
    }
  }

  const columns: ColumnOverrideSpec[] = [];
  for (const [key, draft] of Object.entries(drafts)) {
    const sep = key.indexOf('\0');
    if (sep <= 0) continue;
    const table = key.slice(0, sep);
    const sourceColumn = key.slice(sep + 1);
    if (!sourceColumn) continue;
    if (analysis) {
      const known = validColumnsByTable.get(table);
      if (!known || !known.has(sourceColumn)) continue;
    }

    const trimmedTarget = draft.targetColumn?.trim();
    const validTarget =
      trimmedTarget !== undefined && trimmedTarget !== '' && isValidTargetColumnName(trimmedTarget)
        ? trimmedTarget
        : undefined;

    if (validTarget === undefined && draft.unique === undefined && draft.masking === undefined) {
      continue;
    }

    const spec: ColumnOverrideSpec = {
      table,
      source_column: sourceColumn,
      ...(validTarget !== undefined ? { target_column: validTarget } : {}),
      ...(draft.unique !== undefined ? { unique: draft.unique } : {}),
      ...(draft.masking !== undefined ? { masking: draft.masking } : {}),
    };
    columns.push(spec);
  }

  return {
    columns,
    orphan_policy: settings.orphanPolicy,
    include_audit_columns: settings.includeAuditColumns,
    include_management_tables: settings.includeManagementTables,
  };
}
