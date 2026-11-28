import { useEffect, useId, useState, type FormEvent, type Key, type Ref } from 'react';
import { FileTrigger, Tab, TabList, TabPanel, Tabs } from 'react-aria-components';
import { Button, DataTable, Details, ErrorNotice, Icon } from '../../components/index.ts';
import type { AppError } from '../../lib/errors.ts';
import type {
  DocIndexBuildResult,
  DocSectionContent,
  DocTableQueryResult,
  EvidenceHitInfo,
  SearchEvidencePack,
} from './api.ts';
import { resultCopy } from './copy.ts';
import { baseFileName, formatPageRange, pageUnitFor } from './formats.ts';
import {
  analyzeHitMatch,
  collectHighlightTerms,
  extractSurroundingPassage,
  partitionHitsByRelevance,
} from './highlight.ts';
import { HighlightedText } from './HighlightedText.tsx';
import type { BuildPhase } from './state.ts';
import styles from './ResultPanel.module.css';

export type SearchSubTabId = 'results' | 'outline' | 'tables';

export interface ResultPanelProps {
  headingRef: Ref<HTMLHeadingElement>;
  buildPhase: BuildPhase;
  isStale: boolean;
  hasFiles: boolean;
  index: DocIndexBuildResult | null;
  docFilterId: string;
  error: AppError | null;
  noticeError: AppError | null;
  onDismissNotice: () => void;
  onPickOther: (files: File[]) => void;
  onRetryBuild: () => void;
  queryInput: string;
  onQueryInputChange: (query: string) => void;
  onSubmitSearch: () => void;
  searching: boolean;
  searchResult: SearchEvidencePack | null;
  selectedHitId: string | null;
  onSelectHit: (sectionId: string) => void;
  activeTab: SearchSubTabId;
  onTabChange: (tab: SearchSubTabId) => void;
  selectedSectionId: string | null;
  sectionContent: DocSectionContent | null;
  loadingSection: boolean;
  onSelectSection: (sectionId: string) => void;
  selectedTableUid: string | null;
  tableSql: string;
  onTableSqlChange: (sql: string) => void;
  tableResult: DocTableQueryResult | null;
  loadingTable: boolean;
  onSelectTable: (tableUid: string) => void;
  onOpenTable: (tableUid: string) => void;
  onRunTableSql: (sql?: string) => void;
  onCopied: () => void;
  hasEvidencePrimaryAction: boolean;
}

/**
 * Center column (`결과`) for the search tab:
 * - Blocking error or initial preparation view before index exists
 * - Search bar (`내용 찾기`) with stale guard and empty validation
 * - Tabs: `찾은 내용` (with low-relevance fold), `목차`, `문서 속 표`
 */
export function ResultPanel({
  headingRef,
  buildPhase,
  isStale,
  hasFiles,
  index,
  docFilterId,
  error,
  noticeError,
  onDismissNotice,
  onPickOther,
  onRetryBuild,
  queryInput,
  onQueryInputChange,
  onSubmitSearch,
  searching,
  searchResult,
  selectedHitId,
  onSelectHit,
  activeTab,
  onTabChange,
  selectedSectionId,
  sectionContent,
  loadingSection,
  onSelectSection,
  selectedTableUid,
  tableSql,
  onTableSqlChange,
  tableResult,
  loadingTable,
  onSelectTable,
  onOpenTable,
  onRunTableSql,
  onCopied,
  hasEvidencePrimaryAction,
}: ResultPanelProps) {
  const [emptyQueryError, setEmptyQueryError] = useState(false);
  const queryInputId = useId();
  const tableSelectId = useId();
  const sqlInputId = useId();

  useEffect(() => {
    if (queryInput.trim() !== '') setEmptyQueryError(false);
  }, [queryInput]);

  if (error && !index) {
    return (
      <div className={styles.root}>
        <header className={styles.header}>
          <h2 ref={headingRef} tabIndex={-1} className={styles.title}>
            {resultCopy.errorTitle}
          </h2>
        </header>
        <ErrorNotice
          error={error}
          actions={
            <>
              <FileTrigger
                onSelect={(list) => {
                  const files = list ? Array.from(list) : [];
                  if (files.length > 0) onPickOther(files);
                }}
              >
                <Button variant="primary">{resultCopy.pickOtherFiles}</Button>
              </FileTrigger>
              {hasFiles ? <Button onPress={onRetryBuild}>{resultCopy.retryPrepare}</Button> : null}
            </>
          }
        />
      </div>
    );
  }

  if (!index) {
    return (
      <div className={styles.root}>
        <header className={styles.header}>
          <h2 ref={headingRef} tabIndex={-1} className={styles.title}>
            {resultCopy.preparingTitle}
          </h2>
        </header>
        <p className={styles.waiting}>
          <Icon name="spinner" />
          <span>{resultCopy.preparingBody}</span>
        </p>
      </div>
    );
  }

  const handleFormSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (queryInput.trim() === '') {
      setEmptyQueryError(true);
      return;
    }
    setEmptyQueryError(false);
    onSubmitSearch();
  };

  const docFormatById = new Map(index.documents.map((d) => [d.docId, d.format]));
  const visibleSections = docFilterId
    ? index.sections.filter((s) => s.docId === docFilterId)
    : index.sections;
  const visibleTables = docFilterId
    ? index.tables.filter((t) => t.docId === docFilterId)
    : index.tables;

  const { primary: primaryHits, lowRelevance: lowHits } = searchResult
    ? partitionHitsByRelevance(searchResult.hits, searchResult.query)
    : { primary: [] as EvidenceHitInfo[], lowRelevance: [] as EvidenceHitInfo[] };

  const highlightTerms = searchResult
    ? collectHighlightTerms(
        searchResult.query,
        searchResult.hits.flatMap((h) => h.matchedTerms),
      )
    : [];

  const selectedTable =
    visibleTables.find((t) => t.tableUid === selectedTableUid) ?? visibleTables[0] ?? null;
  const selectedTableFile = selectedTable ? baseFileName(selectedTable.sourcePath) : '';
  const matchedSectionContent =
    sectionContent && (!selectedSectionId || sectionContent.section.sectionId === selectedSectionId)
      ? sectionContent
      : null;
  const matchedTableResult =
    selectedTable && tableResult && tableResult.tableUid === selectedTable.tableUid
      ? tableResult
      : null;

  const renderHitCard = (hit: EvidenceHitInfo) => {
    const fileName = baseFileName(hit.sourcePath);
    const unit = pageUnitFor(docFormatById.get(hit.docId) ?? fileName);
    const pageLabel = formatPageRange(hit.pageStart, hit.pageEnd, unit);
    const sectionHeading = hit.sectionPath.length > 0 ? hit.sectionPath.join(' › ') : hit.title;
    const matchInfo = analyzeHitMatch(hit, searchResult?.query ?? '');
    const hitTerms = collectHighlightTerms(searchResult?.query ?? '', hit.matchedTerms);
    const { passage } = extractSurroundingPassage(hit.fullText, hitTerms, {
      maxChars: 280,
      contextSentences: 1,
    });
    const isSelected = hit.sectionId === selectedHitId;

    return (
      <li key={hit.sectionId} className={styles.hitListItem}>
        <button
          type="button"
          aria-pressed={isSelected}
          onClick={() => onSelectHit(hit.sectionId)}
          className={`${styles.hitCard} ${isSelected ? styles.hitCardSelected : ''}`}
        >
          <span className={styles.hitTitle}>
            <HighlightedText text={sectionHeading} terms={hitTerms} />
          </span>
          <span className={`${styles.hitMeta} tabular-nums`}>
            <span>{fileName}</span>
            <span>·</span>
            <span>{pageLabel}</span>
            <span>·</span>
            <span>{resultCopy.results.matchLabel(matchInfo, hit.outlineBoost)}</span>
            {hit.tableIds.length > 0 ? (
              <>
                <span>·</span>
                <span>{resultCopy.results.embeddedTables(hit.tableIds.length)}</span>
              </>
            ) : null}
          </span>
          <span className={styles.hitExcerpt}>
            <HighlightedText text={passage} terms={hitTerms} />
          </span>
        </button>
      </li>
    );
  };

  return (
    <div className={styles.root}>
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <h2 ref={headingRef} tabIndex={-1} className={styles.title}>
            {resultCopy.headerTitle(
              index.summary.documentsIndexed,
              index.summary.sectionsIndexed,
              index.summary.tablesIndexed,
            )}
          </h2>
          {isStale && buildPhase !== 'building' ? (
            <Button variant="primary" onPress={onRetryBuild}>
              {resultCopy.retryPrepare}
            </Button>
          ) : null}
        </div>
      </header>

      {isStale ? (
        <p className={styles.staleBanner}>
          <Icon name="warning" className={styles.staleIcon} />
          <span>{resultCopy.staleNotice}</span>
        </p>
      ) : null}

      {index.summary.warnings.length > 0 ? (
        <div role="status" className={styles.staleBanner}>
          <Icon name="warning" className={styles.staleIcon} />
          <div className={styles.warningsContent}>
            <p className={styles.warningsTitle}>
              {resultCopy.partialWarningsTitle(index.summary.warnings.length)}
            </p>
            <ul className={styles.warningsList}>
              {index.summary.warnings.map((w, i) => (
                <li key={i}>{resultCopy.formatBuildWarning(w)}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      {noticeError ? (
        <ErrorNotice
          error={noticeError}
          actions={<Button onPress={onDismissNotice}>{resultCopy.dismissNotice}</Button>}
        />
      ) : null}

      <form role="search" onSubmit={handleFormSubmit} className={styles.searchForm}>
        <label htmlFor={queryInputId} className={styles.searchLabel}>
          {resultCopy.searchForm.label}
        </label>
        <div className={styles.searchRow}>
          <input
            id={queryInputId}
            type="search"
            value={queryInput}
            disabled={isStale || buildPhase === 'building'}
            placeholder={resultCopy.searchForm.placeholder}
            aria-invalid={emptyQueryError || undefined}
            onChange={(e) => onQueryInputChange(e.target.value)}
            className={styles.searchInput}
          />
          <Button
            type="submit"
            variant={isStale || hasEvidencePrimaryAction ? 'secondary' : 'primary'}
            isDisabled={isStale || buildPhase === 'building' || searching}
            isPending={searching}
            className={styles.searchSubmit}
          >
            {resultCopy.searchForm.submitButton}
          </Button>
        </div>
        {emptyQueryError ? (
          <p role="alert" className={styles.validationError}>
            {resultCopy.searchForm.emptyQueryError}
          </p>
        ) : null}
      </form>

      <p role="status" className={styles.searchingStatus}>
        {searching ? (
          <>
            <Icon name="spinner" />
            <span>{resultCopy.searchForm.searching}</span>
          </>
        ) : null}
      </p>

      <Tabs
        selectedKey={activeTab}
        onSelectionChange={(key: Key) => onTabChange(String(key) as SearchSubTabId)}
        className={styles.tabs}
      >
        <TabList aria-label={resultCopy.tabs.listLabel} className={styles.tabList}>
          <Tab id="results" className={styles.tab}>
            {resultCopy.tabs.results(searchResult ? searchResult.hits.length : null)}
          </Tab>
          <Tab id="outline" className={styles.tab}>
            {resultCopy.tabs.outline(visibleSections.length)}
          </Tab>
          <Tab id="tables" className={styles.tab}>
            {resultCopy.tabs.tables(visibleTables.length)}
          </Tab>
        </TabList>

        <TabPanel id="results" className={styles.panel}>
          {!searchResult ? (
            <div className={styles.emptyBox}>
              <p className={styles.emptyTitle}>{resultCopy.results.promptTitle}</p>
              <p className={styles.emptyBody}>{resultCopy.results.promptBody}</p>
            </div>
          ) : (
            <div className={styles.resultsStack}>
              <p className={`${styles.meta} tabular-nums`}>
                {resultCopy.results.summary(
                  searchResult.hits.length,
                  searchResult.elapsedMs,
                  searchResult.mode,
                )}
              </p>

              {searchResult.hits.length === 0 ? (
                <div className={styles.emptyBox}>
                  <p className={styles.emptyTitle}>{resultCopy.results.emptyTitle}</p>
                  <p className={styles.emptyBody}>{resultCopy.results.emptyBody}</p>
                  <div>
                    <Button onPress={() => onTabChange('outline')}>
                      {resultCopy.results.browseOutline}
                    </Button>
                  </div>
                </div>
              ) : (
                <>
                  <ul className={styles.hitList}>{primaryHits.map(renderHitCard)}</ul>
                  {lowHits.length > 0 ? (
                    <Details
                      summary={resultCopy.results.lowRelevanceToggle(lowHits.length)}
                      className={styles.lowRelevance}
                    >
                      <ul className={styles.hitList}>{lowHits.map(renderHitCard)}</ul>
                    </Details>
                  ) : null}
                </>
              )}

              {searchResult.tableHits.length > 0 ? (
                <section className={styles.relatedTables}>
                  <h3 className={styles.subHeading}>{resultCopy.results.relatedTablesHeading}</h3>
                  <ul className={styles.relatedList}>
                    {searchResult.tableHits.map((tbl) => {
                      const fname = baseFileName(tbl.sourcePath);
                      const unit = pageUnitFor(docFormatById.get(tbl.docId) ?? fname);
                      const pageText = `${tbl.page}${unit}`;
                      return (
                        <li key={tbl.tableUid} className={styles.relatedRow}>
                          <div className={styles.relatedInfo}>
                            <span className={styles.relatedTitle}>
                              {tbl.caption || `${fname} · ${resultCopy.tables.meta(pageText, tbl.rowCount, tbl.colCount)}`}
                            </span>
                            <span className={`${styles.relatedMeta} tabular-nums`}>
                              {`${fname} · ${resultCopy.tables.meta(pageText, tbl.rowCount, tbl.colCount)}`}
                              {tbl.headers.length > 0 ? ` · ${tbl.headers.slice(0, 5).join(', ')}` : ''}
                            </span>
                          </div>
                          <Button onPress={() => onOpenTable(tbl.tableUid)}>
                            {resultCopy.results.openTable}
                          </Button>
                        </li>
                      );
                    })}
                  </ul>
                </section>
              ) : null}
            </div>
          )}
        </TabPanel>

        <TabPanel id="outline" className={styles.panel}>
          <div className={styles.outlineSplit}>
            <div className={styles.outlineListWrap}>
              <h3 className={styles.subHeading}>{resultCopy.outline.listHeading}</h3>
              <ul className={styles.outlineList}>
                {visibleSections.map((sec) => {
                  const fileName = baseFileName(sec.sourcePath);
                  const active = sec.sectionId === selectedSectionId;
                  const indentLevel = Math.min(3, sec.headingLevel);
                  return (
                    <li key={sec.sectionId}>
                      <button
                        type="button"
                        aria-current={active ? 'true' : undefined}
                        style={{ paddingLeft: `calc(var(--space-2) + ${indentLevel * 12}px)` }}
                        onClick={() => onSelectSection(sec.sectionId)}
                        className={`${styles.outlineBtn} ${active ? styles.outlineBtnActive : ''}`}
                      >
                        <span className={styles.outlineTitle}>{sec.title || fileName}</span>
                        {index.documents.length > 1 ? (
                          <span className={styles.outlineDoc}>{fileName}</span>
                        ) : null}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>

            <div className={styles.outlineBodyWrap}>
              {loadingSection ? (
                <p className={styles.waiting}>
                  <Icon name="spinner" />
                  <span>{resultCopy.outline.loading}</span>
                </p>
              ) : matchedSectionContent ? (
                <>
                  <h3 className={styles.outlineHeading}>
                    {matchedSectionContent.section.sectionPath.length > 0
                      ? matchedSectionContent.section.sectionPath.join(' › ')
                      : matchedSectionContent.section.title}
                  </h3>
                  <p className={`${styles.meta} tabular-nums`}>
                    {`${baseFileName(matchedSectionContent.section.sourcePath)} · ${formatPageRange(
                      matchedSectionContent.section.pageStart,
                      matchedSectionContent.section.pageEnd,
                      pageUnitFor(
                        docFormatById.get(matchedSectionContent.section.docId) ??
                          matchedSectionContent.section.sourcePath,
                      ),
                    )} · ${resultCopy.outline.charCount(matchedSectionContent.section.charCount)}`}
                  </p>
                  <pre className={styles.sectionBody}>
                    <HighlightedText text={matchedSectionContent.fullText} terms={highlightTerms} />
                  </pre>
                  {matchedSectionContent.tables.length > 0 ? (
                    <div className={styles.embeddedSection}>
                      <h4 className={styles.subHeading}>{resultCopy.outline.embeddedTablesHeading}</h4>
                      <ul className={styles.relatedList}>
                        {matchedSectionContent.tables.map((tbl) => (
                          <li key={tbl.tableUid} className={styles.relatedRow}>
                            <span className={styles.relatedMeta}>
                              {`${tbl.caption || tbl.tableId} (${tbl.rowCount}×${tbl.colCount})`}
                            </span>
                            <Button onPress={() => onOpenTable(tbl.tableUid)}>
                              {resultCopy.results.openTable}
                            </Button>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                </>
              ) : (
                <p className={styles.emptyBody}>{resultCopy.outline.pickHint}</p>
              )}
            </div>
          </div>
        </TabPanel>

        <TabPanel id="tables" className={styles.panel}>
          {visibleTables.length === 0 ? (
            <div className={styles.emptyBox}>
              <p className={styles.emptyTitle}>{resultCopy.tables.emptyTitle}</p>
              <p className={styles.emptyBody}>{resultCopy.tables.emptyBody}</p>
            </div>
          ) : (
            <div className={styles.tablesStack}>
              {selectedTable ? (
                <div className={styles.tableControls}>
                  <label htmlFor={tableSelectId} className={styles.searchLabel}>
                    {resultCopy.tables.selectLabel}
                  </label>
                  <select
                    id={tableSelectId}
                    value={selectedTable.tableUid}
                    onChange={(e) => onSelectTable(e.target.value)}
                    className={styles.select}
                  >
                    {visibleTables.map((tbl, idx) => {
                      const fname = baseFileName(tbl.sourcePath);
                      const unit = pageUnitFor(docFormatById.get(tbl.docId) ?? fname);
                      const label = tbl.caption
                        ? `${fname} · ${tbl.caption} (${tbl.page}${unit}, ${tbl.rowCount}×${tbl.colCount})`
                        : `${fname} · 표 ${idx + 1} (${tbl.page}${unit}, ${tbl.rowCount}×${tbl.colCount})`;
                      return (
                        <option key={tbl.tableUid} value={tbl.tableUid}>
                          {label}
                        </option>
                      );
                    })}
                  </select>
                  <p className={`${styles.meta} tabular-nums`}>
                    {resultCopy.tables.meta(
                      `${selectedTable.page}${pageUnitFor(
                        docFormatById.get(selectedTable.docId) ?? selectedTableFile,
                      )}`,
                      selectedTable.rowCount,
                      selectedTable.colCount,
                    )}
                    {matchedTableResult
                      ? ` · ${resultCopy.tables.rowCount(
                          matchedTableResult.rows.length,
                          matchedTableResult.totalMatched,
                          matchedTableResult.truncated,
                        )}`
                      : ''}
                  </p>
                </div>
              ) : null}

              {loadingTable ? (
                <p className={styles.waiting}>
                  <Icon name="spinner" />
                  <span>{resultCopy.tables.runningSql}</span>
                </p>
              ) : matchedTableResult ? (
                <DataTable
                  name={selectedTable?.caption || `${selectedTableFile || '문서'} 표`}
                  columns={matchedTableResult.columns}
                  rows={matchedTableResult.rows}
                  onCopied={() => onCopied()}
                />
              ) : null}

              {selectedTable ? (
                <Details summary={resultCopy.tables.sqlSummary}>
                  <div className={styles.sqlBox}>
                    <p className={styles.emptyBody}>{resultCopy.tables.sqlHint}</p>
                    <label htmlFor={sqlInputId} className={styles.searchLabel}>
                      {resultCopy.tables.sqlLabel}
                    </label>
                    <textarea
                      id={sqlInputId}
                      value={tableSql}
                      rows={3}
                      onChange={(e) => onTableSqlChange(e.target.value)}
                      className={styles.sqlTextarea}
                    />
                    <div>
                      <Button isDisabled={loadingTable || isStale} onPress={() => onRunTableSql()}>
                        {resultCopy.tables.runSql}
                      </Button>
                    </div>
                  </div>
                </Details>
              ) : null}
            </div>
          )}
        </TabPanel>
      </Tabs>
    </div>
  );
}
