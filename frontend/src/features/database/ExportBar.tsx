import { useEffect, useRef, useState } from 'react';
import { Button, Icon } from '../../components/index.ts';
import { fetchDownloadFile, saveBlobAsFile, type AnalyzeResult, type DbEngine } from './api.ts';
import { exportCopy } from './copy.ts';
import styles from './ExportBar.module.css';

const MESSAGE_MS = 6000;

export interface ExportBarProps {
  analysis: AnalyzeResult;
  dbEngine: DbEngine;
  unmaskedCount: number;
  canSave: boolean;
  isStale: boolean;
  isAnalyzing: boolean;
  savingDb: boolean;
  bundleExpired: boolean;
  fileRevision: number;
  editRevision: number;
  dbSaveFeedback: { text: string; ok: boolean; seq: number } | null;
  onSaveDb: () => void;
  onZipExported: (fileRevision: number, editRevision: number) => void;
  onBundleMissing: () => void;
  onReanalyze: () => void;
}

/**
 * Bottom export bar for the DB tab:
 * - Engine + unmasked PII summary (or stale notice when files changed)
 * - Secondary [설계안 ZIP으로 저장하기] (from `analysis.bundleId`)
 * - Single primary [DB 파일로 저장하기]
 */
export function ExportBar({
  analysis,
  dbEngine,
  unmaskedCount,
  canSave,
  isStale,
  isAnalyzing,
  savingDb,
  bundleExpired,
  fileRevision,
  editRevision,
  dbSaveFeedback,
  onSaveDb,
  onZipExported,
  onBundleMissing,
  onReanalyze,
}: ExportBarProps) {
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);
  const [savingZip, setSavingZip] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const request = useRef<AbortController | null>(null);
  const messageLine = useRef<HTMLParagraphElement>(null);
  const latestRevRef = useRef({ fileRevision, editRevision });
  latestRevRef.current = { fileRevision, editRevision };

  useEffect(
    () => () => {
      window.clearTimeout(timer.current);
      request.current?.abort();
    },
    [],
  );

  // Cancel any in-flight ZIP download whenever files or edits change.
  useEffect(() => {
    request.current?.abort();
    request.current = null;
    setSavingZip(false);
  }, [fileRevision, editRevision]);

  useEffect(() => {
    if (!dbSaveFeedback) return;
    window.clearTimeout(timer.current);
    setMessage({ text: dbSaveFeedback.text, ok: dbSaveFeedback.ok });
    timer.current = window.setTimeout(() => setMessage(null), MESSAGE_MS);
  }, [dbSaveFeedback]);

  useEffect(() => {
    if (message) messageLine.current?.scrollIntoView({ block: 'nearest' });
  }, [message]);

  const show = (text: string, ok: boolean) => {
    window.clearTimeout(timer.current);
    setMessage({ text, ok });
    timer.current = window.setTimeout(() => setMessage(null), MESSAGE_MS);
  };

  const savePlanZip = async () => {
    if (request.current || !analysis.bundleId || !canSave) return;
    const startFileRev = fileRevision;
    const startEditRev = editRevision;
    const controller = new AbortController();
    request.current = controller;
    window.clearTimeout(timer.current);
    setMessage(null);
    setSavingZip(true);
    const outcome = await fetchDownloadFile(
      analysis.bundleId,
      'autoetl_plan_bundle.zip',
      controller.signal,
    );
    if (request.current === controller) request.current = null;
    setSavingZip(false);
    if (
      latestRevRef.current.fileRevision !== startFileRev ||
      latestRevRef.current.editRevision !== startEditRev
    ) {
      return;
    }
    switch (outcome.type) {
      case 'ok':
        saveBlobAsFile(outcome.blob, outcome.fileName);
        onZipExported(startFileRev, startEditRev);
        show(exportCopy.savedZip(outcome.fileName), true);
        return;
      case 'missing':
        onBundleMissing();
        show(exportCopy.zipMissing, false);
        return;
      case 'failed':
        show(exportCopy.zipFailed, false);
        return;
      default:
        return;
    }
  };

  return (
    <div className={styles.bar}>
      <p className={styles.contents}>
        {isStale
          ? exportCopy.staleNotice
          : exportCopy.summary(dbEngine, analysis.tables.length, unmaskedCount)}
      </p>
      <div className={styles.actions}>
        {analysis.bundleId ? (
          bundleExpired || (isStale && !isAnalyzing) ? (
            <Button onPress={onReanalyze}>{exportCopy.reanalyze}</Button>
          ) : (
            <Button
              isPending={savingZip}
              isDisabled={!canSave || savingDb}
              onPress={() => void savePlanZip()}
            >
              {savingZip ? <Icon name="spinner" /> : null}
              {exportCopy.saveZip}
            </Button>
          )
        ) : null}

        <Button
          variant="primary"
          isPending={savingDb}
          isDisabled={!canSave}
          onPress={onSaveDb}
          className={styles.primarySave}
        >
          {savingDb ? <Icon name="spinner" /> : null}
          {savingDb ? exportCopy.savingDb : exportCopy.saveDb}
        </Button>
      </div>
      <p ref={messageLine} role="status" className={styles.message}>
        {message ? (
          <>
            <Icon
              name={message.ok ? 'check' : 'warning'}
              className={message.ok ? styles.ok : styles.warn}
            />
            {message.text}
          </>
        ) : null}
      </p>
    </div>
  );
}
