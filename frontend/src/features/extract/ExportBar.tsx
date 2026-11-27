import { useEffect, useRef, useState } from 'react';
import { Button, Icon } from '../../components/index.ts';
import { fetchZip, type ParseResult } from './api.ts';
import { exportCopy, rereadCopy } from './copy.ts';
import { zipContents } from './routing.ts';
import styles from './ExportBar.module.css';

const MESSAGE_MS = 6000;

/** How long the temporary link to a saved ZIP stays valid; the browser starts saving at once. */
const REVOKE_MS = 10_000;

/** Saves a blob under `fileName` through a temporary object URL and link. */
function saveBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), REVOKE_MS);
}

export interface ExportBarProps {
  result: ParseResult;
  /** The settings the read used, for what the ZIP holds. */
  extractCrops: boolean;
  includeChunks: boolean;
  /** Markdown for [본문 복사하기]. */
  copyText: string;
  /** The server has surely dropped this ZIP; offer [다시 읽기] instead. */
  expired: boolean;
  /** Called after a copy or a save started (step 3). */
  onExported: () => void;
  /** Called when the ZIP was fetched (the server keeps that ZIP longer). */
  onZipFetched: () => void;
  /** Called when the server answered that it no longer has the ZIP. */
  onZipMissing: () => void;
  onReread: () => void;
}

/**
 * The 내보내기 bar: what the ZIP holds, [본문 복사하기] and the one primary
 * action, [ZIP으로 저장하기]. The button fetches the ZIP before saving it,
 * so a ZIP the server no longer has turns the button into [다시 읽기]
 * instead of a failed download. The same button element stays, so focus
 * stays.
 */
export function ExportBar({
  result,
  extractCrops,
  includeChunks,
  copyText,
  expired,
  onExported,
  onZipFetched,
  onZipMissing,
  onReread,
}: ExportBarProps) {
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);
  const [saving, setSaving] = useState(false);
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

  // A new message (and the expired line above it) can land under the sticky
  // status line; bring it into view. The tab's scroll margin keeps it clear.
  useEffect(() => {
    if (message) messageLine.current?.scrollIntoView({ block: 'nearest' });
  }, [message]);

  const show = (text: string, ok: boolean) => {
    window.clearTimeout(timer.current);
    setMessage({ text, ok });
    timer.current = window.setTimeout(() => setMessage(null), MESSAGE_MS);
  };

  const copyBody = async () => {
    try {
      if (!navigator.clipboard) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(copyText);
      show(exportCopy.copied, true);
      onExported();
    } catch {
      show(exportCopy.copyFailed, false);
    }
  };

  const saveZip = async () => {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    // Clear the last message, so the same message after this try is read out again.
    window.clearTimeout(timer.current);
    setMessage(null);
    setSaving(true);
    const outcome = await fetchZip(result.bundleId, controller.signal);
    request.current = null;
    setSaving(false);
    switch (outcome.type) {
      case 'ok':
        saveBlob(outcome.blob, outcome.fileName);
        onZipFetched();
        onExported();
        show(exportCopy.saveStarted, true);
        return;
      case 'missing':
        onZipMissing();
        show(exportCopy.zipMissing, false);
        return;
      case 'failed':
        show(exportCopy.zipFailed, false);
        return;
      default:
        // Aborted: the bar is gone, nothing to show.
        return;
    }
  };

  return (
    <div className={styles.bar}>
      <p className={styles.contents}>{exportCopy.contents(zipContents(result, { extractCrops, includeChunks }))}</p>
      <div className={styles.actions}>
        <Button onPress={() => void copyBody()}>{exportCopy.copyBody}</Button>
        {/* One primary button in both states, so a missing ZIP keeps focus on it. */}
        {expired ? (
          <Button variant="primary" onPress={onReread}>
            {rereadCopy.button}
          </Button>
        ) : (
          <Button variant="primary" isPending={saving} onPress={() => void saveZip()} className={styles.zip}>
            {saving ? <Icon name="spinner" /> : null}
            {exportCopy.saveZip}
          </Button>
        )}
      </div>
      {expired ? <p className={styles.expired}>{exportCopy.expired}</p> : null}
      {/* Always mounted, so each new message is read out. */}
      <p ref={messageLine} role="status" className={styles.message}>
        {message ? (
          <>
            <Icon name={message.ok ? 'check' : 'warning'} className={message.ok ? styles.ok : styles.warn} />
            {message.text}
          </>
        ) : null}
      </p>
    </div>
  );
}
