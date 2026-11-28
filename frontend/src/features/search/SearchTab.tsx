import { useEffect, useLayoutEffect, useReducer, useRef, useState } from 'react';
import { StatusBar, StepIndicator, Workspace } from '../../components/index.ts';
import { AppError, isAppError, toAppError } from '../../lib/errors.ts';
import { readFileAsBase64 } from '../../lib/files.ts';
import {
  buildDocIndex,
  queryDocIndex,
  queryDocTableSql,
  readDocSection,
  type DocIndexBuildResult,
  type DocSectionContent,
  type DocTableQueryResult,
} from './api.ts';
import { searchErrorCopy } from './copy.ts';
import { DocumentList } from './DocumentList.tsx';
import { EvidencePanel } from './EvidencePanel.tsx';
import { checkCombinedUploadSize, checkSearchFile } from './formats.ts';
import { ResultPanel, type SearchSubTabId } from './ResultPanel.tsx';
import { SettingsPanel } from './SettingsPanel.tsx';
import { StartScreen } from './StartScreen.tsx';
import {
  canSearch,
  initialSearchState,
  isIndexStale,
  searchReducer,
  statusFor,
  stepFor,
  type SearchFileItem,
} from './state.ts';
import styles from './SearchTab.module.css';

type FocusTarget = 'result' | 'start';

const DEFAULT_TABLE_SQL = 'SELECT * FROM tbl LIMIT 50';

/**
 * Entry of the "문서 내용 검색" tab (loaded lazily by App.tsx into
 * `assets/SearchTab.js` and `assets/SearchTab.css`).
 *
 * - Documents auto-prepare as soon as dropped/picked (`POST /api/docindex/build`),
 *   with no separate "검색 준비하기" button.
 * - StepIndicator tracks 1 넣기 → 2 확인·다듬기 → 3 내보내기 (reached when
 *   [찾은 부분 복사하기], [이 목차 항목 복사하기], or table copy succeeds).
 * - No bottom export bar: results have nothing to download as a file; copy lives
 *   in the 근거 panel and tables.
 */
export default function SearchTab() {
  const [state, dispatch] = useReducer(searchReducer, initialSearchState);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [queryInput, setQueryInput] = useState('');
  const [activeTab, setActiveTab] = useState<SearchSubTabId>('results');
  const [selectedSectionId, setSelectedSectionId] = useState<string | null>(null);
  const [sectionContent, setSectionContent] = useState<DocSectionContent | null>(null);
  const [loadingSection, setLoadingSection] = useState(false);
  const [selectedTableUid, setSelectedTableUid] = useState<string | null>(null);
  const [tableSql, setTableSql] = useState(DEFAULT_TABLE_SQL);
  const [tableResult, setTableResult] = useState<DocTableQueryResult | null>(null);
  const [loadingTable, setLoadingTable] = useState(false);

  const nextId = useRef(0);
  const buildSeq = useRef(0);
  const searchSeq = useRef(0);
  const sectionSeq = useRef(0);
  const tableSeq = useRef(0);
  const buildController = useRef<AbortController | null>(null);
  const searchController = useRef<AbortController | null>(null);
  const sectionController = useRef<AbortController | null>(null);
  const tableController = useRef<AbortController | null>(null);

  const rootRef = useRef<HTMLDivElement>(null);
  const statusRef = useRef<HTMLDivElement>(null);
  const resultHeading = useRef<HTMLHeadingElement>(null);
  const startHeading = useRef<HTMLHeadingElement>(null);
  const pendingFocus = useRef<FocusTarget | null>(null);
  const lastFocused = useRef<Element | null>(null);

  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(
    () => () => {
      buildController.current?.abort();
      searchController.current?.abort();
      sectionController.current?.abort();
      tableController.current?.abort();
    },
    [],
  );

  useLayoutEffect(() => {
    const root = rootRef.current;
    const statusEl = statusRef.current;
    if (!root || !statusEl) return undefined;
    const measure = () =>
      root.style.setProperty('--search-status-height', `${statusEl.offsetHeight}px`);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(statusEl);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    const target = pendingFocus.current;
    if (target !== null) {
      pendingFocus.current = null;
      (target === 'result' ? resultHeading.current : startHeading.current)?.focus();
      return;
    }
    const last = lastFocused.current;
    const active = document.activeElement;
    if (last && !last.isConnected && (active === null || active === document.body)) {
      (resultHeading.current ?? startHeading.current)?.focus();
    }
  });

  const resetSubViews = () => {
    sectionController.current?.abort();
    tableController.current?.abort();
    setSelectedSectionId(null);
    setSectionContent(null);
    setLoadingSection(false);
    setSelectedTableUid(null);
    setTableSql(DEFAULT_TABLE_SQL);
    setTableResult(null);
    setLoadingTable(false);
  };

  const buildIndexForFiles = async (
    items: readonly SearchFileItem[],
    fileRevision: number,
    options: { abortInFlightSubRequests?: boolean } = {},
  ): Promise<DocIndexBuildResult | null> => {
    if (items.length === 0) return null;
    buildController.current?.abort();
    if (options.abortInFlightSubRequests !== false) {
      searchController.current?.abort();
      sectionController.current?.abort();
      tableController.current?.abort();
    }
    const controller = new AbortController();
    buildController.current = controller;
    const seq = ++buildSeq.current;

    dispatch({ type: 'startBuild', now: Date.now() });
    try {
      const encoded = await Promise.all(
        items.map(async (item) => ({
          filename: item.name,
          content_base64: await readFileAsBase64(item.file, { signal: controller.signal }),
        })),
      );
      const built = await buildDocIndex({ files: encoded }, { signal: controller.signal });
      if (seq !== buildSeq.current || stateRef.current.fileRevision !== fileRevision) return null;
      if (built.summary.documentsIndexed === 0 && built.documents.length === 0) {
        dispatch({
          type: 'buildFailed',
          error: new AppError({
            kind: 'unreadable',
            ...searchErrorCopy.noDocumentsIndexed,
            detail: built.summary.warnings.join('\n') || undefined,
          }),
          fileRevision,
          now: Date.now(),
        });
        return null;
      }
      dispatch({ type: 'buildSucceeded', result: built, fileRevision, now: Date.now() });
      return built;
    } catch (error) {
      if (controller.signal.aborted || (isAppError(error) && error.kind === 'aborted')) return null;
      if (seq !== buildSeq.current || stateRef.current.fileRevision !== fileRevision) return null;
      dispatch({
        type: 'buildFailed',
        error: toAppError(error),
        fileRevision,
        now: Date.now(),
      });
      return null;
    }
  };

  const rebuildAfterExpired = async (startFileRev: number): Promise<DocIndexBuildResult | null> => {
    const latest = stateRef.current;
    if (latest.files.length === 0 || latest.fileRevision !== startFileRev) return null;
    return buildIndexForFiles(latest.files, startFileRev, { abortInFlightSubRequests: false });
  };

  const addFiles = (incoming: File[], focus?: FocusTarget) => {
    if (incoming.length === 0) return;
    for (const file of incoming) {
      const fileError = checkSearchFile(file);
      if (fileError) {
        if (!stateRef.current.index) pendingFocus.current = 'result';
        dispatch({ type: 'setValidationError', error: fileError });
        return;
      }
    }

    const newItems: SearchFileItem[] = incoming.map((file) => {
      nextId.current += 1;
      return { id: `searchfile-${nextId.current}`, file, name: file.name, size: file.size };
    });

    const combined = [...stateRef.current.files];
    for (const item of newItems) {
      const existingIdx = combined.findIndex((cur) => cur.name === item.name);
      if (existingIdx >= 0) combined[existingIdx] = item;
      else combined.push(item);
    }

    const totalError = checkCombinedUploadSize(combined);
    if (totalError) {
      if (!stateRef.current.index) pendingFocus.current = 'result';
      dispatch({ type: 'setValidationError', error: totalError });
      return;
    }

    if (focus && !stateRef.current.index) pendingFocus.current = focus;
    const nextFileRevision = stateRef.current.fileRevision + 1;
    resetSubViews();
    setActiveTab('results');
    dispatch({ type: 'mergeFiles', items: newItems });
    void buildIndexForFiles(combined, nextFileRevision);
  };

  const removeFile = (id: string) => {
    const remaining = stateRef.current.files.filter((f) => f.id !== id);
    const nextFileRevision = stateRef.current.fileRevision + 1;
    resetSubViews();
    setActiveTab('results');

    if (remaining.length === 0) {
      buildController.current?.abort();
      searchController.current?.abort();
      pendingFocus.current = 'start';
      dispatch({ type: 'removeFile', id });
      return;
    }

    dispatch({ type: 'removeFile', id });
    void buildIndexForFiles(remaining, nextFileRevision);
  };

  const executeSearch = async (
    queryText: string,
    docIdOverride?: string,
    options: { preserveTab?: boolean } = {},
  ) => {
    const cur = stateRef.current;
    const trimmed = queryText.trim();
    if (!trimmed || !canSearch(cur) || !cur.index) return;

    const startFileRev = cur.fileRevision;
    const mySeq = ++searchSeq.current;
    let settled = false;
    searchController.current?.abort();
    const controller = new AbortController();
    searchController.current = controller;
    const effectiveDocId = docIdOverride !== undefined ? docIdOverride : cur.docFilterId;

    dispatch({ type: 'startSearch', query: trimmed, now: Date.now() });

    const runWithIndexId = async (indexId: string) => {
      const latest = stateRef.current;
      const pack = await queryDocIndex(
        {
          index_id: indexId,
          query: trimmed,
          top_k: latest.settings.topK,
          doc_id: effectiveDocId || null,
          reranker: latest.settings.reranker,
          reranker_model:
            latest.settings.reranker === 'onnx'
              ? latest.settings.rerankerModel.trim() || null
              : null,
          synonyms: latest.settings.synonyms.trim() || null,
        },
        { signal: controller.signal },
      );
      if (mySeq !== searchSeq.current || stateRef.current.fileRevision !== startFileRev) {
        settled = true;
        dispatch({ type: 'cancelSearch', now: Date.now() });
        return;
      }
      settled = true;
      dispatch({
        type: 'searchSucceeded',
        result: pack,
        fileRevision: startFileRev,
        now: Date.now(),
      });
      if (!options.preserveTab) setActiveTab('results');
    };

    try {
      await runWithIndexId(cur.index.indexId);
    } catch (error) {
      if (controller.signal.aborted || (isAppError(error) && error.kind === 'aborted')) return;
      if (isAppError(error) && error.kind === 'expired') {
        const rebuilt = await rebuildAfterExpired(startFileRev);
        if (controller.signal.aborted || mySeq !== searchSeq.current) return;
        if (!rebuilt) {
          settled = true;
          dispatch({ type: 'cancelSearch', now: Date.now() });
          return;
        }
        try {
          await runWithIndexId(rebuilt.indexId);
          return;
        } catch (retryError) {
          if (controller.signal.aborted || (isAppError(retryError) && retryError.kind === 'aborted')) {
            return;
          }
          settled = true;
          dispatch({
            type: 'searchFailed',
            error: toAppError(retryError),
            fileRevision: startFileRev,
            now: Date.now(),
          });
          return;
        }
      }
      settled = true;
      dispatch({
        type: 'searchFailed',
        error: toAppError(error),
        fileRevision: startFileRev,
        now: Date.now(),
      });
    } finally {
      if (!settled && mySeq === searchSeq.current && !controller.signal.aborted) {
        dispatch({ type: 'cancelSearch', now: Date.now() });
      }
    }
  };

  const loadSection = async (sectionId: string) => {
    const cur = stateRef.current;
    if (!cur.index || isIndexStale(cur)) return;

    const startFileRev = cur.fileRevision;
    const mySeq = ++sectionSeq.current;
    sectionController.current?.abort();
    const controller = new AbortController();
    sectionController.current = controller;
    setSelectedSectionId(sectionId);
    setLoadingSection(true);

    const runRead = async (indexId: string) => {
      const content = await readDocSection(
        { index_id: indexId, section_id: sectionId },
        { signal: controller.signal },
      );
      if (mySeq !== sectionSeq.current || stateRef.current.fileRevision !== startFileRev) return;
      setSectionContent(content);
    };

    try {
      await runRead(cur.index.indexId);
    } catch (error) {
      if (controller.signal.aborted || (isAppError(error) && error.kind === 'aborted')) return;
      if (isAppError(error) && error.kind === 'expired') {
        const rebuilt = await rebuildAfterExpired(startFileRev);
        if (controller.signal.aborted || mySeq !== sectionSeq.current || !rebuilt) return;
        try {
          await runRead(rebuilt.indexId);
          return;
        } catch (retryErr) {
          if (controller.signal.aborted || (isAppError(retryErr) && retryErr.kind === 'aborted')) {
            return;
          }
          dispatch({
            type: 'searchFailed',
            error: toAppError(retryErr),
            fileRevision: startFileRev,
            now: Date.now(),
          });
          return;
        }
      }
      dispatch({
        type: 'searchFailed',
        error: toAppError(error),
        fileRevision: startFileRev,
        now: Date.now(),
      });
    } finally {
      if (mySeq === sectionSeq.current) setLoadingSection(false);
    }
  };

  const runTableSql = async (tableUid: string, sql: string) => {
    const cur = stateRef.current;
    if (!cur.index || isIndexStale(cur)) return;

    const startFileRev = cur.fileRevision;
    const mySeq = ++tableSeq.current;
    tableController.current?.abort();
    const controller = new AbortController();
    tableController.current = controller;
    setSelectedTableUid(tableUid);
    setLoadingTable(true);

    const runQuery = async (indexId: string) => {
      const res = await queryDocTableSql(
        { index_id: indexId, table_uid: tableUid, sql: sql.trim() || DEFAULT_TABLE_SQL },
        { signal: controller.signal },
      );
      if (mySeq !== tableSeq.current || stateRef.current.fileRevision !== startFileRev) return;
      setTableResult(res);
    };

    try {
      await runQuery(cur.index.indexId);
    } catch (error) {
      if (controller.signal.aborted || (isAppError(error) && error.kind === 'aborted')) return;
      if (isAppError(error) && error.kind === 'expired') {
        const rebuilt = await rebuildAfterExpired(startFileRev);
        if (controller.signal.aborted || mySeq !== tableSeq.current || !rebuilt) return;
        try {
          await runQuery(rebuilt.indexId);
          return;
        } catch (retryErr) {
          if (controller.signal.aborted || (isAppError(retryErr) && retryErr.kind === 'aborted')) {
            return;
          }
          dispatch({
            type: 'searchFailed',
            error: toAppError(retryErr),
            fileRevision: startFileRev,
            now: Date.now(),
          });
          return;
        }
      }
      dispatch({
        type: 'searchFailed',
        error: toAppError(error),
        fileRevision: startFileRev,
        now: Date.now(),
      });
    } finally {
      if (mySeq === tableSeq.current) setLoadingTable(false);
    }
  };

  const syncActiveSubView = (tab: SearchSubTabId, docId: string, resetExisting: boolean) => {
    const cur = stateRef.current;
    if (!cur.index || isIndexStale(cur)) return;

    if (tab === 'outline') {
      const sections = docId
        ? cur.index.sections.filter((s) => s.docId === docId)
        : cur.index.sections;
      const firstSec = sections[0];
      if (
        firstSec &&
        (resetExisting || !sectionContent || !sections.some((s) => s.sectionId === selectedSectionId))
      ) {
        void loadSection(firstSec.sectionId);
      }
    } else if (tab === 'tables') {
      const tables = docId
        ? cur.index.tables.filter((t) => t.docId === docId)
        : cur.index.tables;
      const firstTbl = tables[0];
      if (
        firstTbl &&
        (resetExisting || !tableResult || !tables.some((t) => t.tableUid === selectedTableUid))
      ) {
        setTableSql(DEFAULT_TABLE_SQL);
        void runTableSql(firstTbl.tableUid, DEFAULT_TABLE_SQL);
      }
    }
  };

  const handleTabChange = (tab: SearchSubTabId) => {
    setActiveTab(tab);
    syncActiveSubView(tab, stateRef.current.docFilterId, false);
  };

  const handleDocFilterChange = (docId: string) => {
    dispatch({ type: 'setDocFilter', docId });
    resetSubViews();
    syncActiveSubView(activeTab, docId, true);
    const cur = stateRef.current;
    if (cur.searchResult && cur.query.trim() && canSearch(cur)) {
      void executeSearch(cur.query, docId, { preserveTab: true });
    }
  };

  const settingsPanel = (
    <SettingsPanel
      settings={state.settings}
      onChange={(patch) => dispatch({ type: 'setSettings', patch })}
      isExpanded={settingsOpen}
      onExpandedChange={setSettingsOpen}
      disabled={state.buildPhase === 'building'}
    />
  );

  const stale = isIndexStale(state);
  const status = statusFor(state);
  const showWorkspace = state.files.length > 0 || state.index !== null || state.error !== null;

  const selectedHit =
    state.searchResult?.hits.find((h) => h.sectionId === state.selectedHitId) ??
    state.searchResult?.hits[0] ??
    null;

  const visibleTables = state.index
    ? state.docFilterId
      ? state.index.tables.filter((t) => t.docId === state.docFilterId)
      : state.index.tables
    : [];
  const selectedTable =
    visibleTables.find((t) => t.tableUid === selectedTableUid) ?? visibleTables[0] ?? null;

  const hasEvidencePrimaryAction =
    (activeTab === 'results' && selectedHit !== null) ||
    (activeTab === 'outline' && sectionContent !== null);

  return (
    <div
      ref={rootRef}
      className={styles.root}
      onFocus={(event) => {
        lastFocused.current = event.target;
      }}
    >
      <StepIndicator current={stepFor(state)} />

      {showWorkspace ? (
        <Workspace
          list={
            <DocumentList
              files={state.files}
              onFiles={(files) => addFiles(files)}
              onRemoveFile={removeFile}
              index={state.index}
              docFilterId={state.docFilterId}
              onDocFilterChange={handleDocFilterChange}
              settings={settingsPanel}
              disabled={state.buildPhase === 'building'}
            />
          }
          result={
            <ResultPanel
              headingRef={resultHeading}
              buildPhase={state.buildPhase}
              isStale={stale}
              hasFiles={state.files.length > 0}
              index={state.index}
              docFilterId={state.docFilterId}
              error={state.error}
              noticeError={state.noticeError}
              onDismissNotice={() => dispatch({ type: 'dismissNoticeError' })}
              onPickOther={(files) => addFiles(files, 'result')}
              onRetryBuild={() => void buildIndexForFiles(state.files, state.fileRevision)}
              queryInput={queryInput}
              onQueryInputChange={(val) => setQueryInput(val)}
              onSubmitSearch={() => void executeSearch(queryInput)}
              searching={state.searching}
              searchResult={state.searchResult}
              selectedHitId={state.selectedHitId}
              onSelectHit={(sectionId) => dispatch({ type: 'selectHit', sectionId })}
              activeTab={activeTab}
              onTabChange={handleTabChange}
              selectedSectionId={selectedSectionId}
              sectionContent={sectionContent}
              loadingSection={loadingSection}
              onSelectSection={(sectionId) => void loadSection(sectionId)}
              selectedTableUid={selectedTable?.tableUid ?? null}
              tableSql={tableSql}
              onTableSqlChange={setTableSql}
              tableResult={tableResult}
              loadingTable={loadingTable}
              onSelectTable={(tableUid) => {
                setTableSql(DEFAULT_TABLE_SQL);
                void runTableSql(tableUid, DEFAULT_TABLE_SQL);
              }}
              onOpenTable={(tableUid) => {
                setActiveTab('tables');
                setTableSql(DEFAULT_TABLE_SQL);
                void runTableSql(tableUid, DEFAULT_TABLE_SQL);
              }}
              onRunTableSql={(sql) => {
                const targetUid = selectedTable?.tableUid;
                if (targetUid) void runTableSql(targetUid, sql ?? tableSql);
              }}
              onCopied={() =>
                dispatch({ type: 'markCopied', fileRevision: stateRef.current.fileRevision })
              }
              hasEvidencePrimaryAction={hasEvidencePrimaryAction}
            />
          }
          evidence={
            state.index || state.error ? (
              <EvidencePanel
                index={state.index}
                error={state.error}
                activeTab={activeTab}
                searchResult={state.searchResult}
                selectedHit={selectedHit}
                sectionContent={sectionContent}
                selectedTable={selectedTable}
                onCopied={() =>
                  dispatch({ type: 'markCopied', fileRevision: stateRef.current.fileRevision })
                }
              />
            ) : null
          }
          exportBar={undefined}
          status={null}
        />
      ) : (
        <StartScreen
          headingRef={startHeading}
          onFiles={(files) => addFiles(files, 'result')}
          settings={settingsPanel}
        />
      )}

      <div ref={statusRef} className={styles.status}>
        <StatusBar
          message={status.message}
          tone={status.tone}
          startedAt={status.startedAt}
          endedAt={status.endedAt}
        />
      </div>
    </div>
  );
}
