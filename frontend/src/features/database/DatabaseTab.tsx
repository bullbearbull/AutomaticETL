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
  type AnalyzeResult,
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
  pruneOverridesForTables,
  setOverrideDraft,
  tablesOwnedByFile,
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
  canSaveDatabase,
  databaseReducer,
  initialDatabaseState,
  isAnalysisStale,
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
 * - Save buttons stay disabled until `analysis` matches the current file list,
 *   and late save/download responses for superseded revisions are ignored.
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
  const saveSeq = useRef(0);
  const pendingNameEditRef = useRef(false);

  const rootRef = useRef<HTMLDivElement>(null);
  const statusRef = useRef<HTMLDivElement>(null);
  const resultHeading = useRef<HTMLHeadingElement>(null);
  const startHeading = useRef<HTMLHeadingElement>(null);
  const pendingFocus = useRef<FocusTarget | null>(null);
  const lastFocused = useRef<Element | null>(null);

  const stateRef = useRef(state);
  stateRef.current = state;

  const status = statusFor(state);
  const stale = isAnalysisStale(state);
  const canSave = canSaveDatabase(state);

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
    fileRevision: number,
    previousAnalysis: AnalyzeResult | null = stateRef.current.analysis,
  ) => {
    if (items.length === 0) return;
    window.clearTimeout(debounceTimer.current);
    pendingNameEditRef.current = false;
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
      if (seq !== analyzeSeq.current || stateRef.current.fileRevision !== fileRevision) return;
      dispatch({ type: 'analyzeSucceeded', result, fileRevision, now: Date.now() });
    } catch (error) {
      if (controller.signal.aborted || (isAppError(error) && error.kind === 'aborted')) return;
      if (seq !== analyzeSeq.current || stateRef.current.fileRevision !== fileRevision) return;
      dispatch({ type: 'analyzeFailed', error: toAppError(error), now: Date.now() });
    }
  };

  const runSessionRevalidate = async (
    overrides: Readonly<Record<string, ColumnOverrideDraft>>,
    settings: DatabaseSettings,
  ) => {
    const cur = stateRef.current;
    if (!cur.analysis || isAnalysisStale(cur)) return;
    if (hasInvalidTargetColumnNames(overrides, cur.analysis)) return;

    const startFileRev = cur.fileRevision;
    window.clearTimeout(debounceTimer.current);
    pendingNameEditRef.current = false;
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
      if (seq !== analyzeSeq.current || stateRef.current.fileRevision !== startFileRev) return;
      dispatch({ type: 'analyzeSucceeded', result, fileRevision: startFileRev, now: Date.now() });
    } catch (error) {
      if (controller.signal.aborted || (isAppError(error) && error.kind === 'aborted')) return;
      const latest = stateRef.current;
      if (
        isAppError(error) &&
        error.kind === 'expired' &&
        latest.files.length > 0 &&
        latest.fileRevision === startFileRev
      ) {
        void runUploadAnalyze(
          latest.files,
          latest.settings,
          latest.overrides,
          'revalidate',
          startFileRev,
          latest.analysis,
        );
        return;
      }
      if (seq !== analyzeSeq.current || latest.fileRevision !== startFileRev) return;
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
    if (incoming.length === 0 || stateRef.current.savingDb) return;

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
    const nextFileRevision = stateRef.current.fileRevision + 1;
    dispatch({ type: 'mergeFiles', items: newItems });
    if (activeTab === 'saved') setActiveTab('schema');
    void runUploadAnalyze(
      combined,
      stateRef.current.settings,
      stateRef.current.overrides,
      'initial',
      nextFileRevision,
    );
  };

  const removeFile = (id: string) => {
    if (stateRef.current.savingDb) return;
    const removed = stateRef.current.files.find((f) => f.id === id);
    const remaining = stateRef.current.files.filter((f) => f.id !== id);
    const nextFileRevision = stateRef.current.fileRevision + 1;
    if (remaining.length === 0) {
      window.clearTimeout(debounceTimer.current);
      pendingNameEditRef.current = false;
      analyzeController.current?.abort();
      saveController.current?.abort();
      pendingFocus.current = 'start';
      setActiveTab('schema');
      dispatch({ type: 'removeFile', id });
      return;
    }

    // Prune drafts tied to tables owned by the removed file so they never leak to remaining tables.
    let nextOverrides = stateRef.current.overrides;
    if (removed && stateRef.current.analysis) {
      const removedTables = tablesOwnedByFile(stateRef.current.analysis, removed.name);
      if (removedTables.size > 0) {
        nextOverrides = pruneOverridesForTables(nextOverrides, removedTables);
      }
    }

    dispatch({ type: 'removeFile', id });
    if (activeTab === 'saved') setActiveTab('schema');
    void runUploadAnalyze(
      remaining,
      stateRef.current.settings,
      nextOverrides,
      'revalidate',
      nextFileRevision,
    );
  };

  const handleSettingsChange = (patch: Partial<DatabaseSettings>) => {
    const cur = stateRef.current;
    if (cur.savingDb) return;
    const nextSettings = { ...cur.settings, ...patch };
    dispatch({ type: 'setSettings', patch });

    const affectsAnalysis =
      patch.purpose !== undefined ||
      patch.decomposeStrings !== undefined ||
      patch.orphanPolicy !== undefined ||
      patch.includeAuditColumns !== undefined ||
      patch.includeManagementTables !== undefined;
    if (!affectsAnalysis || cur.files.length === 0) return;

    if (cur.phase === 'analyzing' || isAnalysisStale(cur)) {
      // An initial/replacement upload analysis is still running or failed: re-upload with new settings.
      void runUploadAnalyze(
        cur.files,
        nextSettings,
        cur.overrides,
        cur.analysis ? 'revalidate' : 'initial',
        cur.fileRevision,
        cur.analysis,
      );
    } else if (cur.analysis) {
      void runSessionRevalidate(cur.overrides, nextSettings);
    }
  };

  const handleUpdateColumnName = (table: string, sourceColumn: string, targetColumn: string) => {
    const cur = stateRef.current;
    if (cur.savingDb) return;
    pendingNameEditRef.current = true;
    const nextOverrides = setOverrideDraft(cur.overrides, table, sourceColumn, {
      targetColumn,
    });
    dispatch({ type: 'setOverrides', overrides: nextOverrides });
    if (!hasInvalidTargetColumnNames(nextOverrides, cur.analysis)) {
      scheduleRevalidate(nextOverrides, cur.settings);
    }
  };

  const handleCommitColumnName = () => {
    const cur = stateRef.current;
    if (cur.savingDb || !cur.analysis || !pendingNameEditRef.current) return;
    if (!hasInvalidTargetColumnNames(cur.overrides, cur.analysis)) {
      window.clearTimeout(debounceTimer.current);
      void runSessionRevalidate(cur.overrides, cur.settings);
    }
  };

  const handleToggleUnique = (table: string, sourceColumn: string, unique: boolean) => {
    const cur = stateRef.current;
    if (cur.savingDb) return;
    const nextOverrides = setOverrideDraft(cur.overrides, table, sourceColumn, {
      unique,
    });
    dispatch({ type: 'setOverrides', overrides: nextOverrides });
    void runSessionRevalidate(nextOverrides, cur.settings);
  };

  const handleChangeMasking = (table: string, sourceColumn: string, masking: MaskingPolicy) => {
    const cur = stateRef.current;
    if (cur.savingDb) return;
    const nextOverrides = setOverrideDraft(cur.overrides, table, sourceColumn, {
      masking,
    });
    dispatch({ type: 'setOverrides', overrides: nextOverrides });
    void runSessionRevalidate(nextOverrides, cur.settings);
  };

  const handleResetOverrides = () => {
    const cur = stateRef.current;
    if (cur.savingDb) return;
    pendingNameEditRef.current = false;
    dispatch({ type: 'resetOverrides' });
    void runSessionRevalidate({}, cur.settings);
  };

  const downloadOutcomeFile = async (outcome: LoadDbResult) => {
    if (!outcome.downloadToken) return;
    const startFileRev = stateRef.current.fileRevision;
    const startEditRev = stateRef.current.editRevision;
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
    const latest = stateRef.current;
    if (latest.fileRevision !== startFileRev || latest.editRevision !== startEditRev) return;
    if (dl.type === 'ok') {
      saveBlobAsFile(dl.blob, dl.fileName);
      dispatch({ type: 'markExported', fileRevision: startFileRev, editRevision: startEditRev });
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
    expectedEditRevision: number = stateRef.current.editRevision,
  ) => {
    const cur = stateRef.current;
    if (!cur.analysis || isAnalysisStale(cur) || cur.savingDb) return;
    if (hasInvalidTargetColumnNames(overridesToUse, cur.analysis)) return;

    const startFileRev = cur.fileRevision;
    const mySaveSeq = ++saveSeq.current;
    let saveSettled = false;
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

      const afterLoad = stateRef.current;
      if (
        afterLoad.fileRevision !== startFileRev ||
        afterLoad.editRevision !== expectedEditRevision
      ) {
        saveSettled = true;
        dispatch({ type: 'cancelSaveDb', now: Date.now() });
        return;
      }

      saveSettled = true;
      dispatch({
        type: 'saveDbSucceeded',
        outcome,
        fileRevision: startFileRev,
        editRevision: expectedEditRevision,
        now: Date.now(),
      });
      setActiveTab('saved');

      if (outcome.loadResult.status !== 'failed' && outcome.downloadToken) {
        const dl = await fetchDownloadFile(
          outcome.downloadToken,
          outcome.downloadFilename,
          controller.signal,
        );
        const afterDl = stateRef.current;
        if (
          afterDl.fileRevision !== startFileRev ||
          afterDl.editRevision !== expectedEditRevision
        ) {
          return;
        }
        if (dl.type === 'ok') {
          saveBlobAsFile(dl.blob, dl.fileName);
          dispatch({
            type: 'markExported',
            fileRevision: startFileRev,
            editRevision: expectedEditRevision,
          });
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
      const latest = stateRef.current;
      if (
        isAppError(error) &&
        error.kind === 'expired' &&
        latest.files.length > 0 &&
        latest.fileRevision === startFileRev &&
        latest.editRevision === expectedEditRevision
      ) {
        const seq = ++analyzeSeq.current;
        try {
          const encodedFiles = await Promise.all(
            latest.files.map(async (item) => ({
              filename: item.name,
              content_base64: await readFileAsBase64(item.file, { signal: controller.signal }),
            })),
          );
          const refreshed = await analyzeTables(
            {
              files: encodedFiles,
              options: {
                purpose: latest.settings.purpose,
                decompose_strings: latest.settings.decomposeStrings,
              },
              overrides: buildSchemaOverrideSpec(overridesToUse, latest.settings, latest.analysis),
            },
            { signal: controller.signal },
          );
          const curAfterRefresh = stateRef.current;
          if (
            controller.signal.aborted ||
            seq !== analyzeSeq.current ||
            curAfterRefresh.fileRevision !== startFileRev
          ) {
            if (!controller.signal.aborted && saveSeq.current === mySaveSeq) {
              saveSettled = true;
              dispatch({ type: 'cancelSaveDb', now: Date.now() });
            }
            return;
          }
          dispatch({
            type: 'analyzeSucceeded',
            result: refreshed,
            fileRevision: startFileRev,
            now: Date.now(),
          });
          if (curAfterRefresh.editRevision !== expectedEditRevision) {
            saveSettled = true;
            dispatch({ type: 'cancelSaveDb', now: Date.now() });
            return;
          }
          await runSaveWithSession(refreshed.sessionId, refreshed);
          return;
        } catch (retryError) {
          if (controller.signal.aborted || (isAppError(retryError) && retryError.kind === 'aborted')) {
            return;
          }
          saveSettled = true;
          dispatch({
            type: 'saveDbFailed',
            error: toAppError(retryError),
            fileRevision: startFileRev,
            editRevision: expectedEditRevision,
            now: Date.now(),
          });
          return;
        }
      }
      saveSettled = true;
      dispatch({
        type: 'saveDbFailed',
        error: toAppError(error),
        fileRevision: startFileRev,
        editRevision: expectedEditRevision,
        now: Date.now(),
      });
    } finally {
      if (!saveSettled && saveSeq.current === mySaveSeq && !controller.signal.aborted) {
        dispatch({ type: 'cancelSaveDb', now: Date.now() });
      }
    }
  };

  const handleRequestSaveDb = () => {
    const cur = stateRef.current;
    if (!cur.analysis) return;
    if (hasInvalidTargetColumnNames(cur.overrides, cur.analysis)) {
      pushFeedback(validationCopy.fixIdentifiersFirst, false);
      setActiveTab('schema');
      return;
    }
    if (!canSaveDatabase(cur)) return;
    if (unmaskedForPrompt.length > 0) {
      setPiiDialogOpen(true);
      return;
    }
    void executeSaveDb(cur.overrides, false, cur.editRevision);
  };

  const handleConfirmMaskAndSave = () => {
    const cur = stateRef.current;
    if (!cur.analysis || isAnalysisStale(cur)) return;
    const maskedDrafts = applyMaskToUnmaskedColumns(cur.overrides, unmaskedForPrompt);
    const nextEditRevision = cur.editRevision + 1;
    dispatch({ type: 'setOverrides', overrides: maskedDrafts });
    void executeSaveDb(maskedDrafts, true, nextEditRevision);
  };

  const handleConfirmSaveAsIs = () => {
    const cur = stateRef.current;
    if (!canSaveDatabase(cur)) return;
    void executeSaveDb(cur.overrides, false, cur.editRevision);
  };

  const settingsPanel = (
    <SettingsPanel
      settings={state.settings}
      onChange={handleSettingsChange}
      isExpanded={settingsOpen}
      onExpandedChange={setSettingsOpen}
      disabled={state.savingDb}
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
                  state.analysis ? 'revalidate' : 'initial',
                  state.fileRevision,
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
              savingDb={state.savingDb}
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
                canSave={canSave}
                isStale={stale}
                isAnalyzing={state.phase === 'analyzing'}
                savingDb={state.savingDb}
                bundleExpired={state.bundleExpired}
                fileRevision={state.fileRevision}
                editRevision={state.editRevision}
                dbSaveFeedback={dbFeedback}
                onSaveDb={handleRequestSaveDb}
                onZipExported={(fileRev, editRev) =>
                  dispatch({ type: 'markExported', fileRevision: fileRev, editRevision: editRev })
                }
                onBundleMissing={() => dispatch({ type: 'markBundleExpired' })}
                onReanalyze={() =>
                  void runUploadAnalyze(
                    state.files,
                    state.settings,
                    state.overrides,
                    'revalidate',
                    state.fileRevision,
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
