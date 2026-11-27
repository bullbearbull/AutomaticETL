import { DataTable } from '../../components/index.ts';
import type { ExtractedTable } from './api.ts';
import { isContinuedTable } from './body.ts';
import { resultCopy } from './copy.ts';
import type { PageUnit } from './formats.ts';
import { whereLabel } from './routing.ts';
import styles from './TablesView.module.css';

export interface ExtractTableProps {
  table: ExtractedTable;
  /** 0-based position among the file's tables; the table is named "표 1", "표 2", … */
  index: number;
  caption: string | null;
  continued: boolean;
  onCopied: () => void;
}

/** One table with its caption and [엑셀용으로 복사하기]. Used in 본문 and 표. */
export function ExtractTable({ table, index, caption, continued, onCopied }: ExtractTableProps) {
  return (
    <div className={styles.table}>
      {caption ? <p className={styles.caption}>{caption}</p> : null}
      {continued ? <p className={styles.note}>{resultCopy.continued}</p> : null}
      <DataTable
        name={resultCopy.tableName(index + 1)}
        {...(table.headers.length > 0 ? { columns: table.headers } : {})}
        rows={table.rows}
        onCopied={onCopied}
      />
    </div>
  );
}

export interface TablesViewProps {
  tables: readonly ExtractedTable[];
  unit: PageUnit;
  onCopied: () => void;
}

/** The 표 view: every table in the file, each with where it was and its size. */
export function TablesView({ tables, unit, onCopied }: TablesViewProps) {
  if (tables.length === 0) return <p className={styles.empty}>{resultCopy.noTables}</p>;
  return (
    <ul className={styles.list}>
      {tables.map((table, index) => (
        <li key={table.id} className={styles.item}>
          <p className={styles.meta}>
            {resultCopy.tableMeta(whereLabel(table.page, unit), table.rowCount, table.colCount)}
          </p>
          <ExtractTable
            table={table}
            index={index}
            caption={table.caption}
            continued={isContinuedTable(table.markdown)}
            onCopied={onCopied}
          />
        </li>
      ))}
    </ul>
  );
}
