import { postJson, type RequestOptions } from '../../api/http.ts';
import { AppError, isAppError } from '../../lib/errors.ts';
import { searchErrorCopy } from './copy.ts';

export type RerankerKind = 'none' | 'onnx';

export interface IndexedDocumentInfo {
  docId: string;
  sourcePath: string;
  format: string;
  sha256: string;
  title: string;
  pageCount: number;
  sectionCount: number;
  tableCount: number;
}

export interface SectionNodeInfo {
  sectionId: string;
  docId: string;
  sourcePath: string;
  parentSectionId: string | null;
  headingLevel: number;
  title: string;
  sectionPath: string[];
  pageStart: number;
  pageEnd: number;
  charCount: number;
  tableIds: string[];
}

export interface IndexedTableInfo {
  tableUid: string;
  docId: string;
  sourcePath: string;
  tableId: string;
  page: number;
  caption: string | null;
  rowCount: number;
  colCount: number;
  headers: string[];
}

export interface IndexBuildSummary {
  documentsIndexed: number;
  documentsSkipped: number;
  sectionsIndexed: number;
  tablesIndexed: number;
  elapsedSeconds: number;
  warnings: string[];
}

export interface DocIndexBuildResult {
  indexId: string;
  downloadToken: string | null;
  elapsedSeconds: number;
  summary: IndexBuildSummary;
  documents: IndexedDocumentInfo[];
  sections: SectionNodeInfo[];
  tables: IndexedTableInfo[];
}

export interface EvidenceHitInfo {
  sectionId: string;
  docId: string;
  sourcePath: string;
  title: string;
  sectionPath: string[];
  pageStart: number;
  pageEnd: number;
  fullText: string;
  bm25Score: number;
  outlineBoost: number;
  rerankScore: number | null;
  finalScore: number;
  matchedTerms: string[];
  tableIds: string[];
}

export interface SearchEvidencePack {
  query: string;
  mode: string;
  rerankerModel: string | null;
  floorCalibrated: boolean;
  elapsedMs: number;
  totalCandidates: number;
  hits: EvidenceHitInfo[];
  tableHits: IndexedTableInfo[];
}

export interface DocSectionContent {
  section: SectionNodeInfo;
  fullText: string;
  tables: IndexedTableInfo[];
}

export interface DocTableQueryResult {
  tableUid: string;
  sourcePath: string;
  tableId: string;
  page: number;
  columns: string[];
  rows: string[][];
  totalMatched: number;
  truncated: boolean;
  sqlExecuted: string;
}

export interface DocIndexBuildRequest {
  files: Array<{ filename: string; content_base64: string }>;
}

export interface DocIndexQueryRequest {
  index_id: string;
  query: string;
  top_k: number;
  doc_id?: string | null;
  reranker?: RerankerKind;
  reranker_model?: string | null;
  synonyms?: string | null;
}

export interface DocIndexSectionRequest {
  index_id: string;
  section_id: string;
}

export interface DocIndexTableSqlRequest {
  index_id: string;
  table_uid: string;
  sql: string;
}

type RawRecord = Record<string, unknown>;

function asRecord(value: unknown): RawRecord {
  return typeof value === 'object' && value !== null ? (value as RawRecord) : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : value === undefined || value === null ? fallback : String(value);
}

function optStr(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function num(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function optNum(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function bool(value: unknown): boolean {
  return Boolean(value);
}

function strList(value: unknown): string[] {
  return asArray(value).map((item) => str(item)).filter((item) => item.length > 0);
}

function rawStrList(value: unknown): string[] {
  return asArray(value).map((item) => str(item));
}

function normalizeDocument(raw: unknown): IndexedDocumentInfo {
  const r = asRecord(raw);
  return {
    docId: str(r.doc_id),
    sourcePath: str(r.source_path),
    format: str(r.format),
    sha256: str(r.sha256),
    title: str(r.title),
    pageCount: Math.max(1, num(r.page_count, 1)),
    sectionCount: Math.max(0, num(r.section_count, 0)),
    tableCount: Math.max(0, num(r.table_count, 0)),
  };
}

function normalizeSection(raw: unknown): SectionNodeInfo {
  const r = asRecord(raw);
  return {
    sectionId: str(r.section_id),
    docId: str(r.doc_id),
    sourcePath: str(r.source_path),
    parentSectionId: optStr(r.parent_section_id),
    headingLevel: Math.max(0, num(r.heading_level, 0)),
    title: str(r.title),
    sectionPath: strList(r.section_path),
    pageStart: Math.max(1, num(r.page_start, 1)),
    pageEnd: Math.max(1, num(r.page_end, 1)),
    charCount: Math.max(0, num(r.char_count, 0)),
    tableIds: strList(r.table_ids),
  };
}

function normalizeTable(raw: unknown): IndexedTableInfo {
  const r = asRecord(raw);
  return {
    tableUid: str(r.table_uid),
    docId: str(r.doc_id),
    sourcePath: str(r.source_path),
    tableId: str(r.table_id),
    page: Math.max(1, num(r.page, 1)),
    caption: optStr(r.caption),
    rowCount: Math.max(0, num(r.row_count, 0)),
    colCount: Math.max(0, num(r.col_count, 0)),
    headers: rawStrList(r.headers),
  };
}

function normalizeHit(raw: unknown): EvidenceHitInfo {
  const r = asRecord(raw);
  return {
    sectionId: str(r.section_id),
    docId: str(r.doc_id),
    sourcePath: str(r.source_path),
    title: str(r.title),
    sectionPath: strList(r.section_path),
    pageStart: Math.max(1, num(r.page_start, 1)),
    pageEnd: Math.max(1, num(r.page_end, 1)),
    fullText: str(r.full_text),
    bm25Score: num(r.bm25_score, 0),
    outlineBoost: num(r.outline_boost, 0),
    rerankScore: optNum(r.rerank_score),
    finalScore: num(r.final_score, 0),
    matchedTerms: strList(r.matched_terms),
    tableIds: strList(r.table_ids),
  };
}

function badResponseError(detail: string): AppError {
  return new AppError({
    kind: 'server',
    ...searchErrorCopy.badResponse,
    detail,
  });
}

export function normalizeBuildResponse(raw: unknown): DocIndexBuildResult {
  const r = asRecord(raw);
  const indexId = optStr(r.index_id);
  if (!indexId) {
    throw badResponseError('missing index_id in /api/docindex/build response');
  }
  const summaryRaw = asRecord(r.summary);
  const documents = asArray(r.documents).map(normalizeDocument);
  const sections = asArray(r.sections).map(normalizeSection);
  const tables = asArray(r.tables).map(normalizeTable);
  return {
    indexId,
    downloadToken: optStr(r.download_token),
    elapsedSeconds: Math.max(0, num(r.elapsed_seconds, 0)),
    summary: {
      documentsIndexed: Math.max(0, num(summaryRaw.documents_indexed, documents.length)),
      documentsSkipped: Math.max(0, num(summaryRaw.documents_skipped, 0)),
      sectionsIndexed: Math.max(0, num(summaryRaw.sections_indexed, sections.length)),
      tablesIndexed: Math.max(0, num(summaryRaw.tables_indexed, tables.length)),
      elapsedSeconds: Math.max(0, num(summaryRaw.elapsed_seconds, 0)),
      warnings: strList(summaryRaw.warnings),
    },
    documents,
    sections,
    tables,
  };
}

export function normalizeQueryResponse(raw: unknown): SearchEvidencePack {
  const r = asRecord(raw);
  if (!Array.isArray(r.hits)) {
    throw badResponseError('missing hits list in /api/docindex/query response');
  }
  return {
    query: str(r.query),
    mode: str(r.mode, 'fts5_bm25'),
    rerankerModel: optStr(r.reranker_model),
    floorCalibrated: bool(r.floor_calibrated),
    elapsedMs: Math.max(0, num(r.elapsed_ms, 0)),
    totalCandidates: Math.max(0, num(r.total_candidates, r.hits.length)),
    hits: r.hits.map(normalizeHit),
    tableHits: asArray(r.table_hits).map(normalizeTable),
  };
}

export function normalizeSectionResponse(raw: unknown): DocSectionContent {
  const r = asRecord(raw);
  const section = normalizeSection(r.section);
  if (!section.sectionId) {
    throw badResponseError('missing section.section_id in /api/docindex/section response');
  }
  return {
    section,
    fullText: str(r.full_text),
    tables: asArray(r.tables).map(normalizeTable),
  };
}

export function normalizeTableSqlResponse(raw: unknown): DocTableQueryResult {
  const r = asRecord(raw);
  if (!Array.isArray(r.columns) || !Array.isArray(r.rows)) {
    throw badResponseError('missing columns/rows in /api/docindex/table-sql response');
  }
  return {
    tableUid: str(r.table_uid),
    sourcePath: str(r.source_path),
    tableId: str(r.table_id),
    page: Math.max(1, num(r.page, 1)),
    columns: rawStrList(r.columns),
    rows: r.rows.map((row) => rawStrList(row)),
    totalMatched: Math.max(0, num(r.total_matched, r.rows.length)),
    truncated: bool(r.truncated),
    sqlExecuted: str(r.sql_executed),
  };
}

export async function buildDocIndex(
  payload: DocIndexBuildRequest,
  options: RequestOptions = {},
): Promise<DocIndexBuildResult> {
  const raw = await postJson<unknown>('/api/docindex/build', payload, options);
  return normalizeBuildResponse(raw);
}

export async function queryDocIndex(
  payload: DocIndexQueryRequest,
  options: RequestOptions = {},
): Promise<SearchEvidencePack> {
  const raw = await postJson<unknown>('/api/docindex/query', payload, options);
  return normalizeQueryResponse(raw);
}

export async function readDocSection(
  payload: DocIndexSectionRequest,
  options: RequestOptions = {},
): Promise<DocSectionContent> {
  const raw = await postJson<unknown>('/api/docindex/section', payload, options);
  return normalizeSectionResponse(raw);
}

export async function queryDocTableSql(
  payload: DocIndexTableSqlRequest,
  options: RequestOptions = {},
): Promise<DocTableQueryResult> {
  try {
    const raw = await postJson<unknown>('/api/docindex/table-sql', payload, options);
    return normalizeTableSqlResponse(raw);
  } catch (error) {
    if (isAppError(error) && error.kind === 'request' && error.status === 400) {
      throw new AppError({
        kind: 'request',
        ...searchErrorCopy.sqlFailed,
        detail: error.detail,
        status: error.status,
      });
    }
    throw error;
  }
}
