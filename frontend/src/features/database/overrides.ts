import type { AnalyzeResult, ColumnMappingInfo, ColumnOverrideSpec, MaskingPolicy, SchemaOverrideSpec } from './api.ts';
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

export type MappingRowKind = 'editable' | 'surrogate' | 'decomposed';

export interface ClassifiedMapping {
  mapping: ColumnMappingInfo;
  kind: MappingRowKind;
}

/**
 * Classifies each mapping row in `tablePlan.columnMappings` so that generated
 * surrogate PK rows (`__surrogate__`) and decomposed string slot rows share
 * deterministic UI identity by `targetColumn` and stay read-only instead of
 * colliding on `sourceColumn`.
 */
export function classifyTableMappings(mappings: readonly ColumnMappingInfo[]): ClassifiedMapping[] {
  const seenSourceColumns = new Set<string>();
  return mappings.map((mapping) => {
    if (
      mapping.sourceColumn === '__surrogate__' ||
      mapping.transform === 'generated_always_as_identity'
    ) {
      return { mapping, kind: 'surrogate' };
    }
    const isSlotTransform =
      mapping.transform.startsWith('NULLIF(regexp_extract(') ||
      mapping.transform.includes('regexp_extract(') ||
      mapping.transform.includes('split_part(') ||
      mapping.transform.includes('substr(');
    if (seenSourceColumns.has(mapping.sourceColumn) || isSlotTransform) {
      return { mapping, kind: 'decomposed' };
    }
    seenSourceColumns.add(mapping.sourceColumn);
    return { mapping, kind: 'editable' };
  });
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

function editableColumnSet(analysis: AnalyzeResult): Map<string, Set<string>> {
  const map = new Map<string, Set<string>>();
  for (const plan of analysis.loadPlan.tablePlans) {
    const editable = new Set<string>();
    for (const item of classifyTableMappings(plan.columnMappings)) {
      if (item.kind === 'editable') editable.add(item.mapping.sourceColumn);
    }
    map.set(plan.sourceTable, editable);
  }
  return map;
}

/**
 * True when any edited DB column name violates the server identifier rule.
 * When `analysis` is passed, drafts belonging to removed tables/columns are ignored.
 */
export function hasInvalidTargetColumnNames(
  drafts: Readonly<Record<string, ColumnOverrideDraft>>,
  analysis: AnalyzeResult | null = null,
): boolean {
  const editableMap = analysis ? editableColumnSet(analysis) : null;
  for (const [key, draft] of Object.entries(drafts)) {
    if (draft.targetColumn === undefined || isValidTargetColumnName(draft.targetColumn)) continue;
    if (editableMap) {
      const sep = key.indexOf('\0');
      if (sep <= 0) continue;
      const table = key.slice(0, sep);
      const col = key.slice(sep + 1);
      if (!editableMap.get(table)?.has(col)) continue;
    }
    return true;
  }
  return false;
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
export function countActiveOverrides(
  drafts: Readonly<Record<string, ColumnOverrideDraft>>,
  analysis: AnalyzeResult | null = null,
): number {
  const editableMap = analysis ? editableColumnSet(analysis) : null;
  let total = 0;
  for (const [key, draft] of Object.entries(drafts)) {
    const hasValue =
      (draft.targetColumn !== undefined && draft.targetColumn.trim() !== '') ||
      draft.unique !== undefined ||
      draft.masking !== undefined;
    if (!hasValue) continue;
    if (editableMap) {
      const sep = key.indexOf('\0');
      if (sep <= 0) continue;
      const table = key.slice(0, sep);
      const col = key.slice(sep + 1);
      if (!editableMap.get(table)?.has(col)) continue;
    }
    total += 1;
  }
  return total;
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

/** Returns the set of `source.tableName` values owned by `fileName` in `analysis`. */
export function tablesOwnedByFile(analysis: AnalyzeResult | null, fileName: string): Set<string> {
  const tables = new Set<string>();
  if (!analysis) return tables;
  for (const t of analysis.tables) {
    if (
      t.source.sourcePath === fileName ||
      t.source.displayName === fileName ||
      t.source.displayName.startsWith(`${fileName} `)
    ) {
      tables.add(t.source.tableName);
    }
  }
  return tables;
}

/** Removes drafts belonging to source tables no longer present in `removedTableNames`. */
export function pruneOverridesForTables(
  drafts: Readonly<Record<string, ColumnOverrideDraft>>,
  removedTableNames: ReadonlySet<string>,
): Record<string, ColumnOverrideDraft> {
  if (removedTableNames.size === 0) return { ...drafts };
  const next: Record<string, ColumnOverrideDraft> = {};
  for (const [key, draft] of Object.entries(drafts)) {
    const sep = key.indexOf('\0');
    const table = sep > 0 ? key.slice(0, sep) : '';
    if (!removedTableNames.has(table)) next[key] = draft;
  }
  return next;
}

/**
 * Builds the `SchemaOverrideSpec` payload sent to `/api/analyze-tables` and
 * `/api/etl/load-db`. Drops overrides referencing surrogate/decomposed columns,
 * columns absent from the current analysis, or invalid target column names.
 */
export function buildSchemaOverrideSpec(
  drafts: Readonly<Record<string, ColumnOverrideDraft>>,
  settings: Pick<DatabaseSettings, 'orphanPolicy' | 'includeAuditColumns' | 'includeManagementTables'>,
  analysis: AnalyzeResult | null,
): SchemaOverrideSpec {
  const validColumnsByTable = analysis ? editableColumnSet(analysis) : null;

  const columns: ColumnOverrideSpec[] = [];
  for (const [key, draft] of Object.entries(drafts)) {
    const sep = key.indexOf('\0');
    if (sep <= 0) continue;
    const table = key.slice(0, sep);
    const sourceColumn = key.slice(sep + 1);
    if (!sourceColumn) continue;
    if (validColumnsByTable) {
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
