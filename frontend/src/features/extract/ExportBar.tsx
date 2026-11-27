import { useEffect, useRef, useState } from 'react';
import { Button, Icon } from '../../components/index.ts';
import { downloadPath, type ParseResult } from './api.ts';
import { exportCopy, rereadCopy } from './copy.ts';
import { zipContents } from './routing.ts';
import styles from './ExportBar.module.css';

const MESSAGE_MS = 6000;

export interface ExportBarProps {
  result: ParseResult;
  /** The settings the read used, for what the ZIP holds. */
  extractCrops: boolean;
  includeChunks: boolean;
  /** Markdown for [본문 복사하기]. */
  copyText: string;
  /** The server has surely dropped this ZIP; offer [다시 읽기] instead of a dead link. */
  expired: boolean;
  /** Called after a copy or a save started (step 3). */
  onExported: () => void;
  /** Called when the ZIP link is used (the server keeps that ZIP longer). */
  onZipFetched: () => void;
  onReread: () => void;
}

/**
 * The 내보내기 bar: what the ZIP holds, [본문 복사하기] and the one primary
 * action, [ZIP으로 저장하기] — a plain link to /api/download/<id>, so the
 * browser saves the file itself.
 */
export function ExportBar({
  result,
  extractCrops,
  includeChunks,
  copyText,
  expired,
  onExported,
  onZipFetched,
  onReread,
}: ExportBarProps) {
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

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

  return (
    <div className={styles.bar}>
      <p className={styles.contents}>{exportCopy.contents(zipContents(result, { extractCrops, includeChunks }))}</p>
      <div className={styles.actions}>
        <Button onPress={() => void copyBody()}>{exportCopy.copyBody}</Button>
        {expired ? (
          <Button variant="primary" onPress={onReread}>
            {rereadCopy.button}
          </Button>
        ) : (
          <a
            href={downloadPath(result.bundleId)}
            download
            className={styles.zip}
            onClick={() => {
              onZipFetched();
              onExported();
              show(exportCopy.saveStarted, true);
            }}
          >
            {exportCopy.saveZip}
          </a>
        )}
      </div>
      {expired ? <p className={styles.expired}>{exportCopy.expired}</p> : null}
      {/* Always mounted, so each new message is read out. */}
      <p role="status" className={styles.message}>
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
