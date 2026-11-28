/**
 * API client and defensive normalizers for `/api/analyze-tables`,
 * `/api/etl/load-db`, and `/api/download/<token>`.
 */
import { postJson, type RequestOptions } from '../../api/http.ts';
import { errorCopy } from '../../copy/common.ts';
import { AppError, isAbortError } from '../../lib/errors.ts';
import { databaseErrorCopy } from './copy.ts';

export type Purpose = 'oltp' | 'analytics' | 'archive';
export type DbEngine = 'sqlite' | 'duckdb';
export type MaskingPolicy = 'none' | 'mask' | 'hash';
export type OrphanPolicy = 'quarantine' | 'reject_all';

export interface ColumnOverrideSpec {
  table: string;
  source_column: string;
  target_column?: string | null;
  unique?: boolean | null;
  masking?: MaskingPolicy;
}

export interface SchemaOverrideSpec {
  columns: ColumnOverrideSpec[];
  orphan_policy: OrphanPolicy;
  include_audit_columns: boolean;
  include_management_tables: boolean;
}

export interface AnalyzeTablesOptions {
  purpose: Purpose;
  decompose_strings: boolean;
}

export interface AnalyzeUploadFile {
  filename: string;
  content_base64: string;
}

export type AnalyzeTablesRequest =
  | {
      files: AnalyzeUploadFile[];
      options: AnalyzeTablesOptions;
      overrides?: SchemaOverrideSpec;
    }
  | {
      session_id: string;
      options: AnalyzeTablesOptions;
      overrides: SchemaOverrideSpec;
    };

export interface LoadDbRequest {
  session_id: string;
  db_engine: DbEngine;
  options: AnalyzeTablesOptions;
  overrides: SchemaOverrideSpec;
  notes: string;
}

export interface TablePatternMask {
  mask: string;
  count: number;
  rate: number;
}

export interface ColumnProfileInfo {
  column: string;
  count: number;
  missingCount: number;
  missingRate: number;
  uniqueCount: number;
  topPatterns: TablePatternMask[];
}

export interface ColumnClassificationInfo {
  column: string;
  physicalType: string;
  semanticType: string;
  category: string;
  missingCount: number;
  missingRate: number;
  uniqueCount: number;
  likelySurrogateIndex: boolean;
  reasons: string[];
}

export interface TableSourceMeta {
  tableId: string;
  tableName: string;
  displayName: string;
  sourcePath: string;
  sourceKind: string;
  sheetName: string | null;
  rowCount: number;
  columnCount: number;
}

export interface AnalyzedTableInfo {
  source: TableSourceMeta;
  encoding: string;
  profiles: ColumnProfileInfo[];
  classifications: ColumnClassificationInfo[];
  warnings: string[];
}

export interface PrimaryKeyInfo {
  tableName: string;
  columns: string[];
  isComposite: boolean;
  recommendSurrogate: boolean;
  surrogateColumnName: string | null;
  referencedByCount: number;
  reasons: string[];
}

export interface ForeignKeyInfo {
  childTable: string;
  childColumns: string[];
  childSlotName: string | null;
  parentTable: string;
  parentColumns: string[];
  cardinality: string;
  inclusionRate: number;
  orphanRows: number;
  reasons: string[];
}

export interface ColumnMappingInfo {
  sourceColumn: string;
  targetTable: string;
  targetColumn: string;
  transform: string;
  typeCast: string;
  nullHandling: string;
}

export interface TableLoadPlanInfo {
  sourceTable: string;
  targetTable: string;
  sourceEncoding: string;
  loadOrderIndex: number;
  dependsOn: string[];
  primaryKey: string[];
  usesSurrogateKey: boolean;
  incrementMode: string;
  columnMappings: ColumnMappingInfo[];
  sensitiveColumns: string[];
  columnMasking: Record<string, MaskingPolicy>;
  confirmedUniqueColumns: string[];
  expectedRowCount: number;
  notes: string[];
}

export interface ScorecardInfo {
  candidateId: string;
  weightedTotal: number;
  integrityScore: number;
  redundancyScore: number;
  fitnessScore: number;
  trialLoadExecuted: boolean;
  trialLoadOk: boolean;
  constraintViolationCount: number;
  castFailureCount: number;
}

export interface LoadOrderItem {
  table: string;
  dependsOn: string[];
}

export interface AnalyzeResult {
  sessionId: string;
  bundleId: string;
  elapsedSeconds: number;
  purpose: Purpose;
  selectedCandidateId: string;
  tables: AnalyzedTableInfo[];
  relations: {
    primaryKeys: Record<string, PrimaryKeyInfo>;
    foreignKeys: ForeignKeyInfo[];
  };
  loadPlan: {
    selectedCandidateId: string;
    scorecards: ScorecardInfo[];
    loadOrder: LoadOrderItem[];
    tablePlans: TableLoadPlanInfo[];
    ddlSql: string;
    indexDdlSql: string;
    commentDdlSql: string;
    erdMermaid: string;
    cycleWarnings: string[];
  };
  managementDdlSql: string;
  analysisReportMarkdown: string;
  warnings: string[];
}

export interface TableLoadOutcomeInfo {
  targetTable: string;
  datasetKind: string;
  sourceName: string;
  rowsRead: number;
  rowsLoaded: number;
  rowsQuarantined: number;
  maskedColumns: string[];
  skippedDuplicateFile: boolean;
}

export interface QuarantineRowInfo {
  targetTable: string;
  sourceRowNumber: number;
  errorColumn: string;
  errorCategory: string;
  errorDetail: string;
}

export interface LoadDbResult {
  downloadToken: string | null;
  downloadFilename: string;
  loadResult: {
    batchId: string;
    loadedAt: string;
    dbEngine: DbEngine;
    status: 'succeeded' | 'partial' | 'failed' | 'skipped_duplicate';
    tables: TableLoadOutcomeInfo[];
    totalRowsRead: number;
    totalRowsLoaded: number;
    totalRowsQuarantined: number;
    warnings: string[];
  };
  quarantine: QuarantineRowInfo[];
  managementDdlSql: string;
}

export async function analyzeTables(request: AnalyzeTablesRequest, options: RequestOptions = {}): Promise<AnalyzeResult> {
  const raw = await postJson<unknown>('/api/analyze-tables', request, options);
  return normalizeAnalyzeResponse(raw);
}

export async function loadDatabase(request: LoadDbRequest, options: RequestOptions = {}): Promise<LoadDbResult> {
  const raw = await postJson<unknown>('/api/etl/load-db', request, options);
  return normalizeLoadDbResponse(raw);
}

export function downloadPath(token: string): string {
  return `/api/download/${encodeURIComponent(token)}`;
}

function baseName(name: string): string {
  return (name.split(/[\\/]/).pop() ?? '').trim();
}

export function downloadFileName(contentDisposition: string | null, fallbackName: string): string {
  const header = contentDisposition ?? '';
  const extended = /(?:^|;)\s*filename\*\s*=\s*([^';]*)'[^';]*'([^;]*)/i.exec(header);
  if (extended && (extended[1] ?? '').trim().toLowerCase() === 'utf-8') {
    try {
      const name = baseName(decodeURIComponent((extended[2] ?? '').trim()));
      if (name) return name;
    } catch {
      // Malformed percent encoding: fall through.
    }
  }
  const plain = /(?:^|;)\s*filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;\s]+))/i.exec(header);
  if (plain) {
    const name = baseName((plain[1] ?? plain[2] ?? '').replace(/\\(.)/g, '$1'));
    if (name) return name;
  }
  return fallbackName;
}

export type DownloadOutcome =
  | { type: 'ok'; blob: Blob; fileName: string }
  | { type: 'missing' }
  | { type: 'failed'; status: number | null }
  | { type: 'aborted' };

export interface DownloadDeps {
  fetch: (url: string, init: RequestInit) => Promise<Response>;
}

export const browserDownloadDeps: DownloadDeps = {
  fetch: (url, init) => fetch(url, init),
};

export async function fetchDownloadFile(
  token: string,
  fallbackName: string,
  signal: AbortSignal,
  deps: DownloadDeps = browserDownloadDeps,
): Promise<DownloadOutcome> {
  let response: Response;
  try {
    response = await deps.fetch(downloadPath(token), { credentials: 'same-origin', signal });
  } catch (error) {
    return signal.aborted || isAbortError(error) ? { type: 'aborted' } : { type: 'failed', status: null };
  }
  if (response.status === 404) return { type: 'missing' };
  if (response.status !== 200) return { type: 'failed', status: response.status };
  try {
    const blob = await response.blob();
    if (signal.aborted) return { type: 'aborted' };
    return { type: 'ok', blob, fileName: downloadFileName(response.headers.get('Content-Disposition'), fallbackName) };
  } catch (error) {
    return signal.aborted || isAbortError(error) ? { type: 'aborted' } : { type: 'failed', status: response.status };
  }
}

/** Triggers a local browser file download from an already-fetched Blob. */
export function saveBlobAsFile(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

type Json = Record<string, unknown>;

function rec(value: unknown): Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? (value as Json) : {};
}

function arr(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function optStr(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

function strList(value: unknown): string[] {
  return arr(value).filter((v): v is string => typeof v === 'string');
}

function nonNegInt(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : fallback;
}

function finiteNum(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function purposeFrom(value: unknown): Purpose {
  if (value === 'analytics' || value === 'archive') return value;
  return 'oltp';
}

function maskingFrom(value: unknown): MaskingPolicy {
  if (value === 'mask' || value === 'hash') return value;
  return 'none';
}

function dbEngineFrom(value: unknown): DbEngine {
  return value === 'duckdb' ? 'duckdb' : 'sqlite';
}

function loadStatusFrom(value: unknown): LoadDbResult['loadResult']['status'] {
  if (value === 'partial' || value === 'failed' || value === 'skipped_duplicate') return value;
  return 'succeeded';
}

export function normalizeAnalyzeResponse(raw: unknown): AnalyzeResult {
  const body = rec(raw);
  const sessionId = str(body.session_id).trim();
  const tablesRaw = arr(body.tables);
  if (!sessionId || tablesRaw.length === 0) {
    throw new AppError({
      kind: tablesRaw.length === 0 ? 'unreadable' : 'server',
      ...(tablesRaw.length === 0 ? databaseErrorCopy.noTablesFound : errorCopy.server),
    });
  }

  const tables: AnalyzedTableInfo[] = tablesRaw.map((item, idx) => {
    const t = rec(item);
    const src = rec(t.source);
    const ingest = rec(t.ingest);
    const files = arr(ingest.files).map(rec);
    const encoding = str(files[0]?.encoding, 'utf-8');
    const tableName = str(src.table_name, `table_${idx + 1}`);
    return {
      source: {
        tableId: str(src.table_id, `t${idx}`),
        tableName,
        displayName: str(src.display_name, tableName),
        sourcePath: str(src.source_path, tableName),
        sourceKind: str(src.source_kind, 'csv'),
        sheetName: optStr(src.sheet_name),
        rowCount: nonNegInt(src.row_count),
        columnCount: nonNegInt(src.column_count),
      },
      encoding,
      profiles: arr(t.profiles).map((pItem) => {
        const p = rec(pItem);
        return {
          column: str(p.column),
          count: nonNegInt(p.count),
          missingCount: nonNegInt(p.missing_count),
          missingRate: finiteNum(p.missing_rate),
          uniqueCount: nonNegInt(p.unique_count),
          topPatterns: arr(p.top_patterns).map((mItem) => {
            const m = rec(mItem);
            return {
              mask: str(m.mask),
              count: nonNegInt(m.count),
              rate: finiteNum(m.rate),
            };
          }),
        };
      }),
      classifications: arr(t.classifications).map((cItem) => {
        const c = rec(cItem);
        const indexMeta = rec(c.index_meta);
        return {
          column: str(c.column),
          physicalType: str(c.physical_type, 'string'),
          semanticType: str(c.semantic_type, 'free_text'),
          category: str(c.category, 'free_text'),
          missingCount: nonNegInt(c.missing_count),
          missingRate: finiteNum(c.missing_rate),
          uniqueCount: nonNegInt(c.unique_count),
          likelySurrogateIndex: indexMeta.likely_surrogate_index === true,
          reasons: strList(c.reasons),
        };
      }),
      warnings: strList(t.warnings),
    };
  });

  const rel = rec(body.relations);
  const pksRaw = rec(rel.primary_keys);
  const primaryKeys: Record<string, PrimaryKeyInfo> = {};
  for (const [k, v] of Object.entries(pksRaw)) {
    const pk = rec(v);
    primaryKeys[k] = {
      tableName: str(pk.table_name, k),
      columns: strList(pk.columns),
      isComposite: pk.is_composite === true,
      recommendSurrogate: pk.recommend_surrogate === true,
      surrogateColumnName: optStr(pk.surrogate_column_name),
      referencedByCount: nonNegInt(pk.referenced_by_count),
      reasons: strList(pk.reasons),
    };
  }

  const foreignKeys: ForeignKeyInfo[] = arr(rel.foreign_keys).map((fkItem) => {
    const fk = rec(fkItem);
    return {
      childTable: str(fk.child_table),
      childColumns: strList(fk.child_columns),
      childSlotName: optStr(fk.child_slot_name),
      parentTable: str(fk.parent_table),
      parentColumns: strList(fk.parent_columns),
      cardinality: str(fk.cardinality, 'N:1'),
      inclusionRate: finiteNum(fk.inclusion_rate),
      orphanRows: nonNegInt(fk.orphan_rows),
      reasons: strList(fk.reasons),
    };
  });

  const lp = rec(body.load_plan);
  const scorecards: ScorecardInfo[] = arr(lp.scorecards).map((scItem) => {
    const sc = rec(scItem);
    const val = rec(sc.validation);
    return {
      candidateId: str(sc.candidate_id),
      weightedTotal: finiteNum(sc.weighted_total),
      integrityScore: finiteNum(rec(sc.integrity).score),
      redundancyScore: finiteNum(rec(sc.redundancy).score),
      fitnessScore: finiteNum(rec(sc.fitness).score),
      trialLoadExecuted: val.trial_load_executed === true,
      trialLoadOk: val.trial_load_ok === true,
      constraintViolationCount: nonNegInt(val.constraint_violation_count),
      castFailureCount: nonNegInt(val.cast_failure_count),
    };
  });

  const loadOrder: LoadOrderItem[] = arr(lp.load_order).map((loItem) => {
    if (typeof loItem === 'string') return { table: loItem, dependsOn: [] };
    const lo = rec(loItem);
    return { table: str(lo.table), dependsOn: strList(lo.depends_on) };
  });

  const tablePlans: TableLoadPlanInfo[] = arr(lp.table_plans).map((tpItem) => {
    const tp = rec(tpItem);
    const rawMasking = rec(tp.column_masking);
    const columnMasking: Record<string, MaskingPolicy> = {};
    for (const [col, policy] of Object.entries(rawMasking)) {
      columnMasking[col] = maskingFrom(policy);
    }
    return {
      sourceTable: str(tp.source_table),
      targetTable: str(tp.target_table),
      sourceEncoding: str(tp.source_encoding, 'utf-8'),
      loadOrderIndex: nonNegInt(tp.load_order_index),
      dependsOn: strList(tp.depends_on),
      primaryKey: strList(tp.primary_key),
      usesSurrogateKey: tp.uses_surrogate_key === true,
      incrementMode: str(tp.increment_mode, 'append'),
      columnMappings: arr(tp.column_mappings).map((mItem) => {
        const m = rec(mItem);
        return {
          sourceColumn: str(m.source_column),
          targetTable: str(m.target_table),
          targetColumn: str(m.target_column),
          transform: str(m.transform, 'identity'),
          typeCast: str(m.type_cast, 'varchar'),
          nullHandling: str(m.null_handling, 'null'),
        };
      }),
      sensitiveColumns: strList(tp.sensitive_columns),
      columnMasking,
      confirmedUniqueColumns: strList(tp.confirmed_unique_columns),
      expectedRowCount: nonNegInt(tp.expected_row_count),
      notes: strList(tp.notes),
    };
  });

  return {
    sessionId,
    bundleId: str(body.bundle_id),
    elapsedSeconds: finiteNum(body.elapsed_seconds),
    purpose: purposeFrom(body.purpose),
    selectedCandidateId: str(body.selected_candidate_id, str(lp.selected_candidate_id)),
    tables,
    relations: { primaryKeys, foreignKeys },
    loadPlan: {
      selectedCandidateId: str(lp.selected_candidate_id, str(body.selected_candidate_id)),
      scorecards,
      loadOrder,
      tablePlans,
      ddlSql: str(lp.ddl_sql),
      indexDdlSql: str(lp.index_ddl_sql),
      commentDdlSql: str(lp.comment_ddl_sql),
      erdMermaid: str(lp.erd_mermaid),
      cycleWarnings: strList(lp.cycle_warnings),
    },
    managementDdlSql: str(body.management_ddl_sql),
    analysisReportMarkdown: str(body.analysis_report_markdown),
    warnings: strList(body.warnings),
  };
}

export function normalizeLoadDbResponse(raw: unknown): LoadDbResult {
  const body = rec(raw);
  const lr = rec(body.load_result);
  const engine = dbEngineFrom(lr.db_engine);
  const fallbackName = engine === 'duckdb' ? 'autoetl_data.duckdb' : 'autoetl_data.sqlite';
  const cat = rec(body.catalog);

  return {
    downloadToken: optStr(body.download_token),
    downloadFilename: str(body.download_filename, fallbackName),
    loadResult: {
      batchId: str(lr.batch_id),
      loadedAt: str(lr.loaded_at),
      dbEngine: engine,
      status: loadStatusFrom(lr.status),
      tables: arr(lr.tables).map((tItem) => {
        const t = rec(tItem);
        return {
          targetTable: str(t.target_table),
          datasetKind: str(t.dataset_kind),
          sourceName: str(t.source_name),
          rowsRead: nonNegInt(t.rows_read),
          rowsLoaded: nonNegInt(t.rows_loaded),
          rowsQuarantined: nonNegInt(t.rows_quarantined),
          maskedColumns: strList(t.masked_columns),
          skippedDuplicateFile: t.skipped_duplicate_file === true,
        };
      }),
      totalRowsRead: nonNegInt(lr.total_rows_read),
      totalRowsLoaded: nonNegInt(lr.total_rows_loaded),
      totalRowsQuarantined: nonNegInt(lr.total_rows_quarantined),
      warnings: strList(lr.warnings),
    },
    quarantine: arr(cat.quarantine).map((qItem) => {
      const q = rec(qItem);
      return {
        targetTable: str(q.target_table),
        sourceRowNumber: nonNegInt(q.source_row_number),
        errorColumn: str(q.error_column),
        errorCategory: str(q.error_category),
        errorDetail: str(q.error_detail),
      };
    }),
    managementDdlSql: str(body.management_ddl_sql),
  };
}
