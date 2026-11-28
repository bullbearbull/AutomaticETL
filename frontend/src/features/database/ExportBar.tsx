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
  savingDb: boolean;
  bundleExpired: boolean;
  dbSaveFeedback: { text: string; ok: boolean; seq: number } | null;
  onSaveDb: () => void;
  onZipExported: () => void;
  onBundleMissing: () => void;
  onReanalyze: () => void;
}

/**
 * Bottom export bar for the DB tab:
 * - Engine + unmasked PII summary
 * - Secondary [설계안 ZIP으로 저장하기] (from `analysis.bundleId`)
 * - Single primary [DB 파일로 저장하기] (triggers pre-save PII dialog or builds & downloads DB)
 */
export function ExportBar({
  analysis,
  dbEngine,
  unmaskedCount,
  savingDb,
  bundleExpired,
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

  useEffect(
    () => () => {
      window.clearTimeout(timer.current);
      request.current?.abort();
    },
    [],
  );

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
    if (request.current || !analysis.bundleId) return;
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
    request.current = null;
    setSavingZip(false);
    switch (outcome.type) {
      case 'ok':
        saveBlobAsFile(outcome.blob, outcome.fileName);
        onZipExported();
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
        {exportCopy.summary(dbEngine, analysis.tables.length, unmaskedCount)}
      </p>
      <div className={styles.actions}>
        {analysis.bundleId ? (
          bundleExpired ? (
            <Button onPress={onReanalyze}>표 다시 살펴보기</Button>
          ) : (
            <Button isPending={savingZip} onPress={() => void savePlanZip()}>
              {savingZip ? <Icon name="spinner" /> : null}
              {exportCopy.saveZip}
            </Button>
          )
        ) : null}

        <Button
          variant="primary"
          isPending={savingDb}
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
