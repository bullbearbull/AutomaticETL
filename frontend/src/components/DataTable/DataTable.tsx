import { useEffect, useRef, useState } from 'react';
import { buttonCopy, tableCopy } from '../../copy/common.ts';
import { copyTable, type TableCell, type TableRows } from '../../lib/tableClipboard.ts';
import { Button } from '../Button/Button.tsx';
import { Icon } from '../Icon/Icon.tsx';
import styles from './DataTable.module.css';

export interface DataTableProps {
  /** Visible name, also used in messages: "표 1". */
  name: string;
  /** Header cells. They are copied as the first row. */
  columns?: readonly TableCell[];
  /** Body rows. A missing cell renders empty. */
  rows: TableRows;
  /** Show [엑셀용으로 복사하기]. Defaults to true. */
  copyable?: boolean;
  /** Called after a copy, with which clipboard path worked. */
  onCopied?: (result: 'rich' | 'plain') => void;
}

const MESSAGE_MS = 6000;

/**
 * A table with an [엑셀용으로 복사하기] button. Copy writes TSV plus an HTML
 * table, so Excel keeps the cells; the result is announced politely. Wide
 * tables scroll inside their own focusable box, never the whole page.
 */
export function DataTable({ name, columns, rows, copyable = true, onCopied }: DataTableProps) {
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const width = Math.max(columns?.length ?? 0, ...rows.map((row) => row.length));
  const pad = (row: readonly TableCell[]) =>
    Array.from({ length: width }, (_, index) => {
      const cell = row[index];
      return cell === null || cell === undefined ? '' : String(cell);
    });

  const handleCopy = async () => {
    window.clearTimeout(timer.current);
    try {
      const all = columns ? [columns, ...rows] : rows;
      const result = await copyTable(all, { headerRows: columns ? 1 : 0 });
      setMessage({ text: tableCopy.copied(name), ok: true });
      onCopied?.(result);
    } catch {
      setMessage({ text: tableCopy.copyFailed(name), ok: false });
    }
    timer.current = window.setTimeout(() => setMessage(null), MESSAGE_MS);
  };

  return (
    <figure className={styles.figure}>
      <div className={styles.header}>
        <figcaption className={styles.name}>{name}</figcaption>
        {copyable ? (
          <Button onPress={() => void handleCopy()} className={styles.copy}>
            {buttonCopy.copyForExcel}
          </Button>
        ) : null}
      </div>
      <p role="status" className={styles.message}>
        {message ? (
          <>
            <Icon name={message.ok ? 'check' : 'warning'} className={message.ok ? styles.ok : styles.warn} />
            {message.text}
          </>
        ) : null}
      </p>
      {width === 0 ? (
        <p className={styles.empty}>{tableCopy.empty}</p>
      ) : (
        <div className={styles.scroll} role="region" aria-label={tableCopy.scrollRegion(name)} tabIndex={0}>
          <table className={`${styles.table} tabular-nums`}>
            {columns ? (
              <thead>
                <tr>
                  {pad(columns).map((cell, index) => (
                    <th key={index} scope="col">
                      {cell}
                    </th>
                  ))}
                </tr>
              </thead>
            ) : null}
            <tbody>
              {rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {pad(row).map((cell, index) => (
                    <td key={index}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </figure>
  );
}
