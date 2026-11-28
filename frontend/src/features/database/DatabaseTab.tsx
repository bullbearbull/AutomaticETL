import { useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react';
import {
  ConfirmDialog,
  StatusBar,
  StepIndicator,
  Workspace,
} from '../../components/index.ts';
import { isAppError, toAppError } from '../../lib/errors.ts';
import { readFileAsBase64 } from '../../lib/files.ts';
import {
  analyzeTables,
  fetchDownloadFile,
  loadDatabase,
  saveBlobAsFile,
  type LoadDbResult,
  type MaskingPolicy,
} from './api.ts';
import { exportCopy, piiDialogCopy, validationCopy } from './copy.ts';
import { EvidencePanel } from './EvidencePanel.tsx';
import { ExportBar } from './ExportBar.tsx';
import { checkCombinedUploadSize, checkDatabaseFile } from './formats.ts';
import {
  applyMaskToUnmaskedColumns,
  buildSchemaOverrideSpec,
  hasInvalidTargetColumnNames,
  setOverrideDraft,
  type ColumnOverrideDraft,
} from './overrides.ts';
import {
  formatPiiPreviewList,
  groupPiiByTable,
  unmaskedPiiColumns,
} from './pii.ts';
import { ResultPanel, type ResultTabId } from './ResultPanel.tsx';
import { SettingsPanel } from './SettingsPanel.tsx';
import { StartScreen } from './StartScreen.tsx';
import {
  databaseReducer,
  initialDatabaseState,
  statusFor,
  stepFor,
  type DatabaseFileItem,
  type DatabaseSettings,
} from './state.ts';
import { TableList } from './TableList.tsx';
import styles from './DatabaseTab.module.css';

type FocusTarget = 'result' | 'start';

const REVALIDATE_DEBOUNCE_MS = 400;

/**
 * Entry of the "엑셀·CSV로 DB 만들기" tab (loaded lazily by App.tsx into
 * `assets/DatabaseTab.js` and `assets/DatabaseTab.css`).
 *
 * - Files are analyzed as soon as dropped/picked (`POST /api/analyze-tables`).
 * - Inline edits to DB column name, unique constraint, or masking policy
 *   automatically re-validate via `session_id` without a separate Apply button.
 * - `[DB 파일로 저장하기]` opens a `ConfirmDialog` before saving if any PII
 *   column is still `"none"` (unless purpose is `"archive"`), then builds and
 *   downloads the DB file in one step.
 */
export default function DatabaseTab() {
  const [state, dispatch] = useReducer(databaseReducer, initialDatabaseState);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<ResultTabId>('schema');
  const [piiDialogOpen, setPiiDialogOpen] = useState(false);
  const [redownloadingDb, setRedownloadingDb] = useState(false);
  const [dbFeedback, setDbFeedback] = useState<{ text: string; ok: boolean; seq: number } | null>(
    null,
  );

  const nextId = useRef(0);
  const feedbackSeq = useRef(0);
  const analyzeController = useRef<AbortController | null>(null);
  const saveController = useRef<AbortController | null>(null);
  const redownloadController = useRef<AbortController | null>(null);
  const debounceTimer = useRef<number | undefined>(undefined);
  const analyzeSeq = useRef(0);

  const rootRef = useRef<HTMLDivElement>(null);
  const statusRef = useRef<HTMLDivElement>(null);
  const resultHeading = useRef<HTMLHeadingElement>(null);
  const startHeading = useRef<HTMLHeadingElement>(null);
  const pendingFocus = useRef<FocusTarget | null>(null);
  const lastFocused = useRef<Element | null>(null);

  const stateRef = useRef(state);
  stateRef.current = state;

  const status = statusFor(state);
  const unmaskedPii = useMemo(
    () => unmaskedPiiColumns(state.analysis, state.overrides),
    [state.analysis, state.overrides],
  );
  const unmaskedForPrompt =
    state.settings.purpose === 'archive' ? [] : unmaskedPii;

  useEffect(
    () => () => {
      window.clearTimeout(debounceTimer.current);
      analyzeController.current?.abort();
      saveController.current?.abort();
      redownloadController.current?.abort();
    },
    [],
  );

  useLayoutEffect(() => {
    const root = rootRef.current;
    const statusEl = statusRef.current;
    if (!root || !statusEl) return undefined;
    const measure = () =>
      root.style.setProperty('--database-status-height', `${statusEl.offsetHeight}px`);
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

  const pushFeedback = (text: string, ok: boolean) => {
    feedbackSeq.current += 1;
    setDbFeedback({ text, ok, seq: feedbackSeq.current });
  };

  const runUploadAnalyze = async (
    items: readonly DatabaseFileItem[],
    settings: DatabaseSettings,
    overrides: Readonly<Record<string, ColumnOverrideDraft>>,
    mode: 'initial' | 'revalidate',
    previousAnalysis = stateRef.current.analysis,
  ) => {
    if (items.length === 0) return;
    window.clearTimeout(debounceTimer.current);
    analyzeController.current?.abort();
    const controller = new AbortController();
    analyzeController.current = controller;
    const seq = ++analyzeSeq.current;

    dispatch({ type: 'startAnalyze', mode, now: Date.now() });
    try {
      const encodedFiles = await Promise.all(
        items.map(async (item) => ({
          filename: item.name,
          content_base64: await readFileAsBase64(item.file, { signal: controller.signal }),
        })),
      );
      const spec = buildSchemaOverrideSpec(overrides, settings, previousAnalysis);
      const result = await analyzeTables(
        {
          files: encodedFiles,
          options: {
            purpose: settings.purpose,
            decompose_strings: settings.decomposeStrings,
          },
          ...(spec.columns.length > 0 ||
          spec.orphan_policy !== 'quarantine' ||
          !spec.include_audit_columns ||
          !spec.include_management_tables
            ? { overrides: spec }
            : {}),
        },
        { signal: controller.signal },
      );
      if (seq !== analyzeSeq.current) return;
      dispatch({ type: 'analyzeSucceeded', result, now: Date.now() });
    } catch (error) {
      if (controller.signal.aborted || (isAppError(error) && error.kind === 'aborted')) return;
      if (seq !== analyzeSeq.current) return;
      dispatch({ type: 'analyzeFailed', error: toAppError(error), now: Date.now() });
    }
  };

  const runSessionRevalidate = async (
    overrides: Readonly<Record<string, ColumnOverrideDraft>>,
    settings: DatabaseSettings,
  ) => {
    const cur = stateRef.current;
    if (!cur.analysis) return;
    if (hasInvalidTargetColumnNames(overrides)) return;

    window.clearTimeout(debounceTimer.current);
    analyzeController.current?.abort();
    const controller = new AbortController();
    analyzeController.current = controller;
    const seq = ++analyzeSeq.current;

    dispatch({ type: 'startAnalyze', mode: 'revalidate', now: Date.now() });
    const spec = buildSchemaOverrideSpec(overrides, settings, cur.analysis);

    try {
      const result = await analyzeTables(
        {
          session_id: cur.analysis.sessionId,
          options: {
            purpose: settings.purpose,
            decompose_strings: settings.decomposeStrings,
          },
          overrides: spec,
        },
        { signal: controller.signal },
      );
      if (seq !== analyzeSeq.current) return;
      dispatch({ type: 'analyzeSucceeded', result, now: Date.now() });
    } catch (error) {
      if (controller.signal.aborted || (isAppError(error) && error.kind === 'aborted')) return;
      if (isAppError(error) && error.kind === 'expired' && cur.files.length > 0) {
        // Server restarted or evicted LRU session: transparently re-upload in-memory files once.
        void runUploadAnalyze(cur.files, settings, overrides, 'revalidate', cur.analysis);
        return;
      }
      if (seq !== analyzeSeq.current) return;
      dispatch({ type: 'analyzeFailed', error: toAppError(error), now: Date.now() });
    }
  };

  const scheduleRevalidate = (
    overrides: Readonly<Record<string, ColumnOverrideDraft>>,
    settings: DatabaseSettings,
  ) => {
    window.clearTimeout(debounceTimer.current);
    debounceTimer.current = window.setTimeout(() => {
      void runSessionRevalidate(overrides, settings);
    }, REVALIDATE_DEBOUNCE_MS);
  };

  const addFiles = (incoming: File[], focus?: FocusTarget) => {
    if (incoming.length === 0) return;

    for (const file of incoming) {
      const fileError = checkDatabaseFile(file);
      if (fileError) {
        if (!stateRef.current.analysis) pendingFocus.current = 'result';
        dispatch({ type: 'setValidationError', error: fileError });
        return;
      }
    }

    const newItems: DatabaseFileItem[] = incoming.map((file) => {
      nextId.current += 1;
      return { id: `dbfile-${nextId.current}`, file, name: file.name, size: file.size };
    });

    const combined = [...stateRef.current.files];
    for (const item of newItems) {
      const existingIdx = combined.findIndex((cur) => cur.name === item.name);
      if (existingIdx >= 0) combined[existingIdx] = item;
      else combined.push(item);
    }

    const totalError = checkCombinedUploadSize(combined);
    if (totalError) {
      if (!stateRef.current.analysis) pendingFocus.current = 'result';
      dispatch({ type: 'setValidationError', error: totalError });
      return;
    }

    if (focus && !stateRef.current.analysis) pendingFocus.current = focus;
    dispatch({ type: 'mergeFiles', items: newItems });
    if (activeTab === 'saved') setActiveTab('schema');
    void runUploadAnalyze(combined, stateRef.current.settings, stateRef.current.overrides, 'initial');
  };

  const removeFile = (id: string) => {
    const remaining = stateRef.current.files.filter((f) => f.id !== id);
    if (remaining.length === 0) {
      window.clearTimeout(debounceTimer.current);
      analyzeController.current?.abort();
      saveController.current?.abort();
      pendingFocus.current = 'start';
      setActiveTab('schema');
      dispatch({ type: 'removeFile', id });
      return;
    }
    dispatch({ type: 'removeFile', id });
    void runUploadAnalyze(
      remaining,
      stateRef.current.settings,
      stateRef.current.overrides,
      'revalidate',
    );
  };

  const handleSettingsChange = (patch: Partial<DatabaseSettings>) => {
    const nextSettings = { ...stateRef.current.settings, ...patch };
    dispatch({ type: 'setSettings', patch });
    const affectsAnalysis =
      patch.purpose !== undefined ||
      patch.decomposeStrings !== undefined ||
      patch.orphanPolicy !== undefined ||
      patch.includeAuditColumns !== undefined ||
      patch.includeManagementTables !== undefined;
    if (affectsAnalysis && stateRef.current.analysis) {
      void runSessionRevalidate(stateRef.current.overrides, nextSettings);
    }
  };

  const handleUpdateColumnName = (table: string, sourceColumn: string, targetColumn: string) => {
    const nextOverrides = setOverrideDraft(stateRef.current.overrides, table, sourceColumn, {
      targetColumn,
    });
    dispatch({ type: 'setOverrides', overrides: nextOverrides });
    if (!hasInvalidTargetColumnNames(nextOverrides)) {
      scheduleRevalidate(nextOverrides, stateRef.current.settings);
    }
  };

  const handleCommitColumnName = () => {
    if (!stateRef.current.analysis) return;
    if (!hasInvalidTargetColumnNames(stateRef.current.overrides)) {
      window.clearTimeout(debounceTimer.current);
      void runSessionRevalidate(stateRef.current.overrides, stateRef.current.settings);
    }
  };

  const handleToggleUnique = (table: string, sourceColumn: string, unique: boolean) => {
    const nextOverrides = setOverrideDraft(stateRef.current.overrides, table, sourceColumn, {
      unique,
    });
    dispatch({ type: 'setOverrides', overrides: nextOverrides });
    void runSessionRevalidate(nextOverrides, stateRef.current.settings);
  };

  const handleChangeMasking = (table: string, sourceColumn: string, masking: MaskingPolicy) => {
    const nextOverrides = setOverrideDraft(stateRef.current.overrides, table, sourceColumn, {
      masking,
    });
    dispatch({ type: 'setOverrides', overrides: nextOverrides });
    void runSessionRevalidate(nextOverrides, stateRef.current.settings);
  };

  const handleResetOverrides = () => {
    dispatch({ type: 'resetOverrides' });
    void runSessionRevalidate({}, stateRef.current.settings);
  };

  const downloadOutcomeFile = async (outcome: LoadDbResult) => {
    if (!outcome.downloadToken) return;
    redownloadController.current?.abort();
    const controller = new AbortController();
    redownloadController.current = controller;
    setRedownloadingDb(true);
    const dl = await fetchDownloadFile(
      outcome.downloadToken,
      outcome.downloadFilename,
      controller.signal,
    );
    setRedownloadingDb(false);
    if (dl.type === 'ok') {
      saveBlobAsFile(dl.blob, dl.fileName);
      dispatch({ type: 'markExported' });
      pushFeedback(exportCopy.savedDb(dl.fileName), true);
    } else if (dl.type === 'missing') {
      pushFeedback(exportCopy.dbDownloadMissing, false);
    } else if (dl.type === 'failed') {
      pushFeedback(exportCopy.dbDownloadFailed, false);
    }
  };

  const executeSaveDb = async (
    overridesToUse: Readonly<Record<string, ColumnOverrideDraft>>,
    shouldReanalyzeAfter: boolean,
  ) => {
    const cur = stateRef.current;
    if (!cur.analysis || cur.savingDb) return;

    window.clearTimeout(debounceTimer.current);
    saveController.current?.abort();
    const controller = new AbortController();
    saveController.current = controller;
    dispatch({ type: 'startSaveDb', now: Date.now() });

    const runSaveWithSession = async (sessionId: string, analysisSnapshot = cur.analysis!) => {
      const spec = buildSchemaOverrideSpec(overridesToUse, cur.settings, analysisSnapshot);
      const outcome = await loadDatabase(
        {
          session_id: sessionId,
          db_engine: cur.settings.dbEngine,
          options: {
            purpose: cur.settings.purpose,
            decompose_strings: cur.settings.decomposeStrings,
          },
          overrides: spec,
          notes: cur.settings.notes,
        },
        { signal: controller.signal },
      );
      dispatch({ type: 'saveDbSucceeded', outcome, now: Date.now() });
      setActiveTab('saved');

      if (outcome.loadResult.status !== 'failed' && outcome.downloadToken) {
        const dl = await fetchDownloadFile(
          outcome.downloadToken,
          outcome.downloadFilename,
          controller.signal,
        );
        if (dl.type === 'ok') {
          saveBlobAsFile(dl.blob, dl.fileName);
          pushFeedback(exportCopy.savedDb(dl.fileName), true);
        } else if (dl.type === 'missing') {
          pushFeedback(exportCopy.dbDownloadMissing, false);
        } else if (dl.type === 'failed') {
          pushFeedback(exportCopy.dbDownloadFailed, false);
        }
      }

      if (shouldReanalyzeAfter) {
        void runSessionRevalidate(overridesToUse, cur.settings);
      }
    };

    try {
      await runSaveWithSession(cur.analysis.sessionId);
    } catch (error) {
      if (controller.signal.aborted || (isAppError(error) && error.kind === 'aborted')) return;
      if (isAppError(error) && error.kind === 'expired' && cur.files.length > 0) {
        // Re-create session seamlessly if evicted on server, then retry load-db once.
        try {
          const encodedFiles = await Promise.all(
            cur.files.map(async (item) => ({
              filename: item.name,
              content_base64: await readFileAsBase64(item.file, { signal: controller.signal }),
            })),
          );
          const refreshed = await analyzeTables(
            {
              files: encodedFiles,
              options: {
                purpose: cur.settings.purpose,
                decompose_strings: cur.settings.decomposeStrings,
              },
              overrides: buildSchemaOverrideSpec(overridesToUse, cur.settings, cur.analysis),
            },
            { signal: controller.signal },
          );
          dispatch({ type: 'analyzeSucceeded', result: refreshed, now: Date.now() });
          await runSaveWithSession(refreshed.sessionId, refreshed);
          return;
        } catch (retryError) {
          if (controller.signal.aborted) return;
          dispatch({ type: 'saveDbFailed', error: toAppError(retryError), now: Date.now() });
          return;
        }
      }
      dispatch({ type: 'saveDbFailed', error: toAppError(error), now: Date.now() });
    }
  };

  const handleRequestSaveDb = () => {
    if (!state.analysis) return;
    if (hasInvalidTargetColumnNames(state.overrides)) {
      pushFeedback(validationCopy.fixIdentifiersFirst, false);
      setActiveTab('schema');
      return;
    }
    if (unmaskedForPrompt.length > 0) {
      setPiiDialogOpen(true);
      return;
    }
    void executeSaveDb(state.overrides, false);
  };

  const handleConfirmMaskAndSave = () => {
    const maskedDrafts = applyMaskToUnmaskedColumns(state.overrides, unmaskedForPrompt);
    dispatch({ type: 'setOverrides', overrides: maskedDrafts });
    void executeSaveDb(maskedDrafts, true);
  };

  const handleConfirmSaveAsIs = () => {
    void executeSaveDb(state.overrides, false);
  };

  const settingsPanel = (
    <SettingsPanel
      settings={state.settings}
      onChange={handleSettingsChange}
      isExpanded={settingsOpen}
      onExpandedChange={setSettingsOpen}
    />
  );

  const showWorkspace = state.files.length > 0 || state.analysis !== null || state.error !== null;
  const piiGroups = groupPiiByTable(unmaskedForPrompt);
  const piiExamples = formatPiiPreviewList(unmaskedForPrompt);

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
            <TableList
              files={state.files}
              onFiles={(files) => addFiles(files)}
              onRemoveFile={removeFile}
              analysis={state.analysis}
              selectedTable={state.selectedTable}
              onSelectTable={(tableName) => dispatch({ type: 'selectTable', tableName })}
              overrides={state.overrides}
              purpose={state.settings.purpose}
              onPurposeChange={(purpose) => handleSettingsChange({ purpose })}
              settings={settingsPanel}
              disabled={state.savingDb}
            />
          }
          result={
            <ResultPanel
              headingRef={resultHeading}
              phase={state.phase}
              analysis={state.analysis}
              selectedTable={state.selectedTable}
              error={state.error}
              noticeError={state.noticeError}
              onDismissNotice={() => dispatch({ type: 'dismissNoticeError' })}
              hasFiles={state.files.length > 0}
              onPickOther={(files) => addFiles(files, 'result')}
              onRetryAnalyze={() =>
                void runUploadAnalyze(
                  state.files,
                  state.settings,
                  state.overrides,
                  'initial',
                )
              }
              overrides={state.overrides}
              onUpdateColumnName={handleUpdateColumnName}
              onCommitColumnName={handleCommitColumnName}
              onToggleUnique={handleToggleUnique}
              onChangeMasking={handleChangeMasking}
              onResetOverrides={handleResetOverrides}
              activeTab={activeTab}
              onTabChange={setActiveTab}
              loadOutcome={state.loadOutcome}
              dbEngine={state.settings.dbEngine}
              onRedownloadDb={(outcome) => void downloadOutcomeFile(outcome)}
              redownloadingDb={redownloadingDb}
            />
          }
          evidence={
            state.analysis || state.error ? (
              <EvidencePanel
                analysis={state.analysis}
                selectedTable={state.selectedTable}
                overrides={state.overrides}
                error={state.error}
              />
            ) : null
          }
          exportBar={
            state.analysis ? (
              <ExportBar
                analysis={state.analysis}
                dbEngine={state.settings.dbEngine}
                unmaskedCount={unmaskedPii.length}
                savingDb={state.savingDb}
                bundleExpired={state.bundleExpired}
                dbSaveFeedback={dbFeedback}
                onSaveDb={handleRequestSaveDb}
                onZipExported={() => dispatch({ type: 'markExported' })}
                onBundleMissing={() => dispatch({ type: 'markBundleExpired' })}
                onReanalyze={() =>
                  void runUploadAnalyze(
                    state.files,
                    state.settings,
                    state.overrides,
                    'revalidate',
                  )
                }
              />
            ) : undefined
          }
          status={null}
        />
      ) : (
        <StartScreen
          headingRef={startHeading}
          onFiles={(files) => addFiles(files, 'result')}
          purpose={state.settings.purpose}
          onPurposeChange={(purpose) => handleSettingsChange({ purpose })}
          settings={settingsPanel}
        />
      )}

      <ConfirmDialog
        isOpen={piiDialogOpen}
        onOpenChange={setPiiDialogOpen}
        title={piiDialogCopy.title(unmaskedForPrompt.length)}
        actions={[
          {
            label: piiDialogCopy.primaryAction,
            primary: true,
            onAction: handleConfirmMaskAndSave,
          },
          {
            label: piiDialogCopy.secondaryAction,
            onAction: handleConfirmSaveAsIs,
          },
        ]}
      >
        <div className={styles.dialogBody}>
          <p>{piiDialogCopy.lead(unmaskedForPrompt.length)}</p>
          <ul className={styles.dialogList}>
            {piiGroups.map((group) => (
              <li key={group.table}>{piiDialogCopy.tableLine(group.table, group.columns)}</li>
            ))}
          </ul>
          {piiExamples ? <p>{piiDialogCopy.previewLine(piiExamples)}</p> : null}
          <p className={styles.dialogNote}>{piiDialogCopy.noteArchiveTip}</p>
        </div>
      </ConfirmDialog>

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
