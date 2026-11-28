import { useEffect, useId, useRef, useState, type Key, type Ref } from 'react';
import { FileTrigger, Tab, TabList, TabPanel, Tabs } from 'react-aria-components';
import { Button, DataTable, Details, ErrorNotice, Icon, MarkdownView } from '../../components/index.ts';
import type { AppError } from '../../lib/errors.ts';
import { physicalTypeLabel, resultCopy, validationCopy, maskingCopy } from './copy.ts';
import {
  countActiveOverrides,
  getOverrideDraft,
  isValidTargetColumnName,
  type ColumnOverrideDraft,
} from './overrides.ts';
import { extractPiiColumns } from './pii.ts';
import type {
  AnalyzeResult,
  DbEngine,
  LoadDbResult,
  MaskingPolicy,
} from './api.ts';
import { formatKindLabel } from './formats.ts';
import styles from './ResultPanel.module.css';

export type ResultTabId = 'schema' | 'relations' | 'plan' | 'saved';

export interface ResultPanelProps {
  headingRef: Ref<HTMLHeadingElement>;
  phase: 'idle' | 'analyzing' | 'revalidating' | 'ready' | 'error';
  analysis: AnalyzeResult | null;
  selectedTable: string | null;
  error: AppError | null;
  noticeError: AppError | null;
  onDismissNotice: () => void;
  hasFiles: boolean;
  onPickOther: (files: File[]) => void;
  onRetryAnalyze: () => void;
  overrides: Readonly<Record<string, ColumnOverrideDraft>>;
  onUpdateColumnName: (table: string, sourceColumn: string, targetColumn: string) => void;
  onCommitColumnName: () => void;
  onToggleUnique: (table: string, sourceColumn: string, unique: boolean) => void;
  onChangeMasking: (table: string, sourceColumn: string, masking: MaskingPolicy) => void;
  onResetOverrides: () => void;
  activeTab: ResultTabId;
  onTabChange: (tab: ResultTabId) => void;
  loadOutcome: LoadDbResult | null;
  dbEngine: DbEngine;
  onRedownloadDb: (outcome: LoadDbResult) => void;
  redownloadingDb: boolean;
}

const MASKING_OPTIONS: readonly MaskingPolicy[] = ['none', 'mask', 'hash'];

function formatLocalTime(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso || '방금 전';
  return parsed.toLocaleString('ko-KR');
}

/**
 * Center column (`결과`) for the DB tab:
 * - Loading & error states (with non-blocking dismissible notice on existing analysis)
 * - Inline editable schema table (`표 구조`) with instant validation & auto re-verify
 * - Relations (`표 연결`), save order + folded SQL/report (`저장 순서·정리`), and save summary (`저장 결과`)
 */
export function ResultPanel({
  headingRef,
  phase,
  analysis,
  selectedTable,
  error,
  noticeError,
  onDismissNotice,
  hasFiles,
  onPickOther,
  onRetryAnalyze,
  overrides,
  onUpdateColumnName,
  onCommitColumnName,
  onToggleUnique,
  onChangeMasking,
  onResetOverrides,
  activeTab,
  onTabChange,
  loadOutcome,
  dbEngine,
  onRedownloadDb,
  redownloadingDb,
}: ResultPanelProps) {
  const [ddlMessage, setDdlMessage] = useState<string | null>(null);
  const ddlTimer = useRef<number | undefined>(undefined);
  const nameHintBaseId = useId();

  useEffect(() => () => window.clearTimeout(ddlTimer.current), []);

  const currentTable =
    analysis?.tables.find((t) => t.source.tableName === selectedTable) ?? analysis?.tables[0] ?? null;
  const currentPlan =
    (currentTable
      ? analysis?.loadPlan.tablePlans.find((tp) => tp.sourceTable === currentTable.source.tableName)
      : undefined) ?? null;

  const effectiveTab: ResultTabId =
    activeTab === 'saved' && !loadOutcome ? 'schema' : activeTab;

  if (error && !analysis) {
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
                allowsMultiple
                onSelect={(list) => {
                  const files = list ? Array.from(list) : [];
                  if (files.length > 0) onPickOther(files);
                }}
              >
                <Button variant="primary">{resultCopy.pickOtherFiles}</Button>
              </FileTrigger>
              {hasFiles ? <Button onPress={onRetryAnalyze}>{resultCopy.retryAnalyze}</Button> : null}
            </>
          }
        />
      </div>
    );
  }

  if (!analysis || !currentTable) {
    return (
      <div className={styles.root}>
        <header className={styles.header}>
          <h2 ref={headingRef} tabIndex={-1} className={styles.title}>
            {resultCopy.loadingTitle}
          </h2>
        </header>
        <p className={styles.waiting}>
          <Icon name="spinner" />
          <span>{resultCopy.loadingBody}</span>
        </p>
      </div>
    );
  }

  const pkInfo = analysis.relations.primaryKeys[currentTable.source.tableName];
  const pkColumns = new Set(currentPlan?.primaryKey ?? pkInfo?.columns ?? []);
  const confirmedUnique = new Set(currentPlan?.confirmedUniqueColumns ?? []);
  const profileMap = new Map(currentTable.profiles.map((p) => [p.column, p]));
  const classMap = new Map(currentTable.classifications.map((c) => [c.column, c]));
  const piiMap = new Map(
    extractPiiColumns(analysis, overrides)
      .filter((item) => item.table === currentTable.source.tableName)
      .map((item) => [item.sourceColumn, item]),
  );

  const combinedDdl = [
    analysis.loadPlan.ddlSql,
    analysis.loadPlan.indexDdlSql,
    analysis.loadPlan.commentDdlSql,
  ]
    .filter((s) => s.trim() !== '')
    .join('\n\n');

  const copyDdl = async () => {
    window.clearTimeout(ddlTimer.current);
    setDdlMessage(null);
    try {
      if (!navigator.clipboard) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(combinedDdl);
      setDdlMessage(resultCopy.plan.copiedDdl);
    } catch {
      setDdlMessage(resultCopy.plan.copyDdlFailed);
    }
    ddlTimer.current = window.setTimeout(() => setDdlMessage(null), 5000);
  };

  const activeOverrideCount = countActiveOverrides(overrides);
  const fkCount = analysis.relations.foreignKeys.length;
  const pkEntries = Object.values(analysis.relations.primaryKeys);

  return (
    <div className={styles.root}>
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <h2 ref={headingRef} tabIndex={-1} className={styles.title}>
            {currentTable.source.displayName}
          </h2>
          {activeOverrideCount > 0 ? (
            <Button variant="quiet" onPress={onResetOverrides} className={styles.resetBtn}>
              {resultCopy.resetOverrides}
            </Button>
          ) : null}
        </div>
        <p className={`${styles.meta} tabular-nums`}>
          {resultCopy.tableSummary(
            formatKindLabel(currentTable.source.sourceKind),
            currentTable.source.rowCount,
            currentTable.source.columnCount,
            currentTable.source.sheetName,
          )}
        </p>
      </header>

      {noticeError ? (
        <ErrorNotice
          error={noticeError}
          actions={<Button onPress={onDismissNotice}>{resultCopy.dismissNotice}</Button>}
        />
      ) : null}

      {phase === 'revalidating' ? (
        <p role="status" className={styles.revalidating}>
          <Icon name="spinner" />
          <span>{resultCopy.revalidatingNotice}</span>
        </p>
      ) : null}

      <Tabs
        selectedKey={effectiveTab}
        onSelectionChange={(key: Key) => onTabChange(String(key) as ResultTabId)}
        className={styles.tabs}
      >
        <TabList aria-label={resultCopy.tabs.schema} className={styles.tabList}>
          <Tab id="schema" className={styles.tab}>
            {resultCopy.tabs.schema}
          </Tab>
          <Tab id="relations" className={styles.tab}>
            {resultCopy.tabs.relations(fkCount)}
          </Tab>
          <Tab id="plan" className={styles.tab}>
            {resultCopy.tabs.plan}
          </Tab>
          {loadOutcome ? (
            <Tab id="saved" className={styles.tab}>
              {resultCopy.tabs.saved}
            </Tab>
          ) : null}
        </TabList>

        <TabPanel id="schema" className={styles.panel}>
          <div
            className={styles.tableScroll}
            role="region"
            aria-label={resultCopy.schemaScrollRegion(currentTable.source.displayName)}
            tabIndex={0}
          >
            <table className={styles.schemaTable}>
              <thead>
                <tr>
                  <th scope="col">{resultCopy.schemaColumns.sourceColumn}</th>
                  <th scope="col">{resultCopy.schemaColumns.targetColumn}</th>
                  <th scope="col">{resultCopy.schemaColumns.valueKind}</th>
                  <th scope="col">{resultCopy.schemaColumns.unique}</th>
                  <th scope="col">{resultCopy.schemaColumns.masking}</th>
                </tr>
              </thead>
              <tbody>
                {(currentPlan?.columnMappings ?? []).map((mapping, idx) => {
                  const col = mapping.sourceColumn;
                  const draft = getOverrideDraft(overrides, currentTable.source.tableName, col);
                  const inputValue = draft.targetColumn !== undefined ? draft.targetColumn : mapping.targetColumn;
                  const validName = isValidTargetColumnName(inputValue);
                  const errorId = `${nameHintBaseId}-${idx}`;

                  const profile = profileMap.get(col);
                  const cls = classMap.get(col);
                  const pii = piiMap.get(col);
                  const isPk = pkColumns.has(col);
                  const effectiveUnique =
                    isPk || (draft.unique !== undefined ? draft.unique : confirmedUnique.has(col));
                  const effectiveMasking: MaskingPolicy =
                    draft.masking ?? currentPlan?.columnMasking[col] ?? 'none';

                  return (
                    <tr key={col}>
                      <td>
                        <div className={styles.colCell}>
                          <span className={styles.sourceColName}>{col}</span>
                          <div className={styles.badgeRow}>
                            {isPk ? <span className={styles.badgePk}>{resultCopy.badges.primaryKey}</span> : null}
                            {pii ? (
                              effectiveMasking === 'none' ? (
                                <span className={styles.badgePiiWarn}>
                                  <Icon name="warning" />
                                  {resultCopy.badges.piiUnmasked}
                                </span>
                              ) : (
                                <span className={styles.badgePiiSafe}>
                                  <Icon name="lock" />
                                  {resultCopy.badges.piiProtected}
                                </span>
                              )
                            ) : null}
                          </div>
                        </div>
                      </td>

                      <td>
                        <div className={styles.nameField}>
                          <input
                            type="text"
                            value={inputValue}
                            aria-label={resultCopy.targetColumnInputLabel(col)}
                            aria-invalid={!validName || undefined}
                            aria-describedby={!validName ? errorId : undefined}
                            onChange={(e) =>
                              onUpdateColumnName(currentTable.source.tableName, col, e.target.value)
                            }
                            onBlur={onCommitColumnName}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') onCommitColumnName();
                            }}
                            className={`${styles.nameInput} ${!validName ? styles.nameInputInvalid : ''}`}
                          />
                          {!validName ? (
                            <span id={errorId} className={styles.nameError}>
                              {validationCopy.invalidIdentifier}
                            </span>
                          ) : null}
                        </div>
                      </td>

                      <td>
                        <div className={styles.kindCell}>
                          <span>
                            {physicalTypeLabel(cls?.physicalType ?? 'string', cls?.semanticType ?? 'free_text')}
                          </span>
                          {profile ? (
                            <span className={`${styles.kindSub} tabular-nums`}>
                              {resultCopy.missingRate(profile.missingCount, profile.missingRate)}
                            </span>
                          ) : null}
                        </div>
                      </td>

                      <td className={styles.checkCell}>
                        <label className={styles.checkWrap}>
                          <input
                            type="checkbox"
                            checked={effectiveUnique}
                            disabled={isPk}
                            aria-label={resultCopy.uniqueInputLabel(col)}
                            onChange={(e) =>
                              onToggleUnique(currentTable.source.tableName, col, e.target.checked)
                            }
                            className={styles.checkbox}
                          />
                        </label>
                      </td>

                      <td>
                        <select
                          value={effectiveMasking}
                          aria-label={resultCopy.maskingInputLabel(col)}
                          onChange={(e) =>
                            onChangeMasking(
                              currentTable.source.tableName,
                              col,
                              e.target.value as MaskingPolicy,
                            )
                          }
                          className={styles.maskSelect}
                        >
                          {MASKING_OPTIONS.map((policy) => (
                            <option key={policy} value={policy}>
                              {maskingCopy[policy].label}
                            </option>
                          ))}
                        </select>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </TabPanel>

        <TabPanel id="relations" className={styles.panel}>
          <div className={styles.sectionStack}>
            <section className={styles.block}>
              <h3 className={styles.blockHeading}>{resultCopy.relations.pkHeading}</h3>
              <ul className={styles.simpleList}>
                {pkEntries.map((pk) => {
                  const plan = analysis.loadPlan.tablePlans.find((tp) => tp.sourceTable === pk.tableName);
                  const surrogate =
                    plan?.usesSurrogateKey || pk.recommendSurrogate
                      ? pk.surrogateColumnName ?? `${pk.tableName}_id`
                      : null;
                  return (
                    <li key={pk.tableName} className={styles.listCard}>
                      <span className={styles.listCardTitle}>
                        {resultCopy.relations.pkRow(pk.tableName, pk.columns, surrogate)}
                      </span>
                      {pk.reasons.length > 0 ? (
                        <span className={styles.listCardMeta}>{pk.reasons.join(' · ')}</span>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            </section>

            <section className={styles.block}>
              <h3 className={styles.blockHeading}>{resultCopy.relations.fkHeading}</h3>
              {fkCount === 0 ? (
                <p className={styles.emptyNote}>{resultCopy.relations.fkNone}</p>
              ) : (
                <ul className={styles.simpleList}>
                  {analysis.relations.foreignKeys.map((fk, idx) => (
                    <li
                      key={`${fk.childTable}-${fk.parentTable}-${idx}`}
                      className={styles.listCard}
                    >
                      <span className={styles.listCardTitle}>
                        {resultCopy.relations.fkCard(
                          fk.childTable,
                          fk.childColumns,
                          fk.parentTable,
                          fk.parentColumns,
                          fk.cardinality,
                        )}
                      </span>
                      <span className={`${styles.listCardMeta} tabular-nums`}>
                        {resultCopy.relations.fkMatch(fk.inclusionRate, fk.orphanRows)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        </TabPanel>

        <TabPanel id="plan" className={styles.panel}>
          <div className={styles.sectionStack}>
            {analysis.loadPlan.cycleWarnings.length > 0 ? (
              <div className={styles.warningBox}>
                <Icon name="warning" className={styles.warningIcon} />
                <div>
                  <p>{resultCopy.plan.cycleWarning}</p>
                  <ul className={styles.rawList}>
                    {analysis.loadPlan.cycleWarnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                </div>
              </div>
            ) : null}

            <section className={styles.block}>
              <h3 className={styles.blockHeading}>{resultCopy.plan.orderHeading}</h3>
              <ol className={styles.orderList}>
                {(analysis.loadPlan.loadOrder.length > 0
                  ? analysis.loadPlan.loadOrder
                  : analysis.loadPlan.tablePlans.map((tp) => ({
                      table: tp.targetTable,
                      dependsOn: tp.dependsOn,
                    }))
                ).map((item, index) => {
                  const tp = analysis.loadPlan.tablePlans.find(
                    (plan) => plan.targetTable === item.table || plan.sourceTable === item.table,
                  );
                  return (
                    <li key={item.table} className={styles.listCard}>
                      <span className={styles.listCardTitle}>
                        {resultCopy.plan.orderItem(
                          index,
                          item.table,
                          tp?.expectedRowCount ?? 0,
                          item.dependsOn,
                        )}
                      </span>
                    </li>
                  );
                })}
              </ol>
            </section>

            <Details summary={resultCopy.plan.detailsSummary}>
              <div className={styles.detailsStack}>
                {combinedDdl ? (
                  <div className={styles.codeSection}>
                    <div className={styles.codeHeader}>
                      <h4 className={styles.subHeading}>{resultCopy.plan.ddlHeading}</h4>
                      <Button onPress={() => void copyDdl()}>{resultCopy.plan.copyDdl}</Button>
                    </div>
                    {ddlMessage ? (
                      <p role="status" className={styles.inlineStatus}>
                        {ddlMessage}
                      </p>
                    ) : null}
                    <pre className={styles.codeBlock}>{combinedDdl}</pre>
                  </div>
                ) : null}

                {analysis.managementDdlSql.trim() !== '' ? (
                  <div className={styles.codeSection}>
                    <h4 className={styles.subHeading}>{resultCopy.plan.managementDdlHeading}</h4>
                    <pre className={styles.codeBlock}>{analysis.managementDdlSql}</pre>
                  </div>
                ) : null}

                {analysis.loadPlan.erdMermaid.trim() !== '' ? (
                  <div className={styles.codeSection}>
                    <h4 className={styles.subHeading}>{resultCopy.plan.erdHeading}</h4>
                    <pre className={styles.codeBlock}>{analysis.loadPlan.erdMermaid}</pre>
                  </div>
                ) : null}

                {analysis.loadPlan.scorecards.length > 0 ? (
                  <DataTable
                    name={resultCopy.plan.scorecardHeading}
                    copyable={false}
                    columns={['방식', '종합 점수', '무결성', '중복 감소', '모의 저장']}
                    rows={analysis.loadPlan.scorecards.map((sc) => [
                      sc.candidateId,
                      `${Math.round(sc.weightedTotal * 10) / 10}`,
                      `${Math.round(sc.integrityScore * 10) / 10}`,
                      `${Math.round(sc.redundancyScore * 10) / 10}`,
                      sc.trialLoadOk ? '통과' : '주의 필요',
                    ])}
                  />
                ) : null}

                {analysis.analysisReportMarkdown.trim() !== '' ? (
                  <div className={styles.codeSection}>
                    <h4 className={styles.subHeading}>{resultCopy.plan.reportHeading}</h4>
                    <MarkdownView source={analysis.analysisReportMarkdown} />
                  </div>
                ) : null}
              </div>
            </Details>
          </div>
        </TabPanel>

        {loadOutcome ? (
          <TabPanel id="saved" className={styles.panel}>
            <div className={styles.sectionStack}>
              <div
                className={
                  loadOutcome.loadResult.status === 'failed' ? styles.warningBox : styles.doneBox
                }
              >
                <Icon
                  name={
                    loadOutcome.loadResult.status === 'succeeded' ||
                    loadOutcome.loadResult.status === 'skipped_duplicate'
                      ? 'check'
                      : loadOutcome.loadResult.status === 'partial'
                        ? 'warning'
                        : 'error'
                  }
                  className={
                    loadOutcome.loadResult.status === 'failed'
                      ? styles.errorIcon
                      : loadOutcome.loadResult.status === 'partial'
                        ? styles.warningIcon
                        : styles.doneIcon
                  }
                />
                <div className={styles.saveText}>
                  <p className={styles.saveTitle}>
                    {loadOutcome.loadResult.status === 'failed'
                      ? resultCopy.saved.failedTitle
                      : resultCopy.saved.heading}
                  </p>
                  <p>
                    {loadOutcome.loadResult.status === 'failed'
                      ? resultCopy.saved.failedBody
                      : resultCopy.saved.summary(
                          loadOutcome.loadResult.totalRowsLoaded,
                          loadOutcome.loadResult.totalRowsRead,
                          loadOutcome.loadResult.totalRowsQuarantined,
                        )}
                  </p>
                  {loadOutcome.loadResult.status === 'skipped_duplicate' ? (
                    <p className={styles.listCardMeta}>{resultCopy.saved.skippedDuplicate}</p>
                  ) : null}
                  {loadOutcome.loadResult.status === 'partial' ? (
                    <p className={styles.listCardMeta}>{resultCopy.saved.partialNote}</p>
                  ) : null}
                  <p className={`${styles.listCardMeta} tabular-nums`}>
                    {resultCopy.saved.meta(
                      (loadOutcome.loadResult.dbEngine || dbEngine) === 'duckdb'
                        ? 'DuckDB (.duckdb)'
                        : 'SQLite (.sqlite)',
                      formatLocalTime(loadOutcome.loadResult.loadedAt),
                      loadOutcome.downloadFilename,
                    )}
                  </p>
                </div>
                {loadOutcome.downloadToken ? (
                  <Button
                    isPending={redownloadingDb}
                    onPress={() => onRedownloadDb(loadOutcome)}
                  >
                    {resultCopy.saved.redownload}
                  </Button>
                ) : null}
              </div>

              {loadOutcome.loadResult.tables.length > 0 ? (
                <DataTable
                  name={resultCopy.saved.tableHeading}
                  columns={[...resultCopy.saved.tableColumns]}
                  rows={loadOutcome.loadResult.tables.map((t) => [
                    t.targetTable,
                    t.sourceName,
                    t.rowsRead,
                    t.rowsLoaded,
                    t.rowsQuarantined,
                    t.maskedColumns.length > 0 ? t.maskedColumns.join(', ') : '없음',
                  ])}
                />
              ) : null}

              {loadOutcome.quarantine.length > 0 ? (
                <DataTable
                  name={resultCopy.saved.quarantineHeading(loadOutcome.quarantine.length)}
                  columns={[...resultCopy.saved.quarantineColumns]}
                  rows={loadOutcome.quarantine.map((q) => [
                    q.targetTable,
                    q.sourceRowNumber,
                    q.errorColumn,
                    q.errorCategory,
                    q.errorDetail,
                  ])}
                />
              ) : null}
            </div>
          </TabPanel>
        ) : null}
      </Tabs>
    </div>
  );
}
