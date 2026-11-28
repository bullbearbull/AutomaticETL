import { useId, useRef, type ReactNode } from 'react';
import { ListBox, ListBoxItem, useDrop, type DropItem } from 'react-aria-components';
import { Button, FileDropZone, Icon } from '../../components/index.ts';
import { formatBytes } from '../../lib/files.ts';
import type { AnalyzeResult, Purpose } from './api.ts';
import { listCopy } from './copy.ts';
import type { ColumnOverrideDraft } from './overrides.ts';
import { extractPiiColumns } from './pii.ts';
import { PurposeSelector } from './PurposeSelector.tsx';
import type { DatabaseFileItem } from './state.ts';
import styles from './TableList.module.css';

export interface TableListProps {
  files: readonly DatabaseFileItem[];
  onFiles: (files: File[]) => void;
  onRemoveFile: (id: string) => void;
  analysis: AnalyzeResult | null;
  selectedTable: string | null;
  onSelectTable: (tableName: string) => void;
  overrides: Readonly<Record<string, ColumnOverrideDraft>>;
  purpose: Purpose;
  onPurposeChange: (purpose: Purpose) => void;
  settings: ReactNode;
  disabled?: boolean;
}

async function filesFrom(items: readonly DropItem[]): Promise<File[]> {
  const files: File[] = [];
  for (const item of items) {
    if (item.kind === 'file') files.push(await item.getFile());
  }
  return files;
}

/**
 * Left column (`목록`) for the DB tab:
 * - Uploaded files with remove buttons and drop support
 * - Analyzed tables ListBox with row×col metadata and PII status badges
 * - DB purpose selector (with immediate `원본 보관용` notice) + 세부 설정
 */
export function TableList({
  files,
  onFiles,
  onRemoveFile,
  analysis,
  selectedTable,
  onSelectTable,
  overrides,
  purpose,
  onPurposeChange,
  settings,
  disabled = false,
}: TableListProps) {
  const filesHeadingId = useId();
  const tablesHeadingId = useId();
  const dropRef = useRef<HTMLDivElement>(null);
  const { dropProps, isDropTarget } = useDrop({
    ref: dropRef,
    getDropOperation: () => 'copy',
    onDrop: (event) => {
      void filesFrom(event.items).then((dropped) => {
        if (dropped.length > 0) onFiles(dropped);
      });
    },
  });

  const totalBytes = files.reduce((sum, item) => sum + item.size, 0);
  const allPii = extractPiiColumns(analysis, overrides);

  const tableItems = (analysis?.tables ?? []).map((table) => {
    const tablePii = allPii.filter((item) => item.table === table.source.tableName);
    const unmaskedCount = tablePii.filter((item) => item.effectiveMasking === 'none').length;
    return {
      id: table.source.tableName,
      displayName: table.source.displayName,
      rows: table.source.rowCount,
      cols: table.source.columnCount,
      totalPii: tablePii.length,
      unmaskedCount,
    };
  });

  return (
    <div className={styles.root}>
      <div {...dropProps} ref={dropRef} className={styles.drop} data-drop-target={isDropTarget || undefined}>
        <div className={styles.sectionHeader}>
          <h2 id={filesHeadingId} className={styles.heading}>
            {listCopy.filesHeading(files.length)}
          </h2>
          {files.length > 0 ? (
            <span className={`${styles.totalSize} tabular-nums`}>{listCopy.totalSize(formatBytes(totalBytes))}</span>
          ) : null}
        </div>

        {files.length > 0 ? (
          <ul aria-labelledby={filesHeadingId} className={styles.fileList}>
            {files.map((item) => (
              <li key={item.id} className={styles.fileRow}>
                <span className={styles.fileInfo}>
                  <span className={styles.fileName}>{item.name}</span>
                  <span className={`${styles.fileSize} tabular-nums`}>{formatBytes(item.size)}</span>
                </span>
                <Button
                  variant="quiet"
                  isDisabled={disabled}
                  onPress={() => onRemoveFile(item.id)}
                  aria-label={listCopy.removeFile(item.name)}
                  className={styles.removeBtn}
                >
                  {listCopy.removeButton}
                </Button>
              </li>
            ))}
          </ul>
        ) : null}

        <FileDropZone variant="compact" onFiles={onFiles} isDisabled={disabled} />
      </div>

      {tableItems.length > 0 ? (
        <div className={styles.tablesSection}>
          <h2 id={tablesHeadingId} className={styles.heading}>
            {listCopy.tablesHeading(tableItems.length)}
          </h2>
          <ListBox
            aria-labelledby={tablesHeadingId}
            items={tableItems}
            selectionMode="single"
            selectionBehavior="replace"
            disallowEmptySelection
            selectedKeys={selectedTable ? [selectedTable] : []}
            onSelectionChange={(keys) => {
              if (keys === 'all') return;
              const [first] = keys;
              if (first !== undefined) onSelectTable(String(first));
            }}
            className={styles.tableList}
          >
            {(item) => {
              const meta = listCopy.tableMeta(item.rows, item.cols);
              const piiText =
                item.unmaskedCount > 0
                  ? listCopy.tableUnmaskedPii(item.unmaskedCount)
                  : item.totalPii > 0
                    ? listCopy.tableAllProtected(item.totalPii)
                    : '';
              const label = piiText ? `${meta}, ${piiText}` : meta;
              return (
                <ListBoxItem
                  id={item.id}
                  textValue={item.displayName}
                  aria-label={listCopy.tableOption(item.displayName, label)}
                  className={styles.tableItem}
                >
                  <span className={styles.tableName}>{item.displayName}</span>{' '}
                  <span className={`${styles.tableMeta} tabular-nums`}>{meta}</span>
                  {item.unmaskedCount > 0 ? (
                    <>
                      {' '}
                      <span className={styles.piiWarn}>
                        <Icon name="warning" />
                        {piiText}
                      </span>
                    </>
                  ) : item.totalPii > 0 ? (
                    <>
                      {' '}
                      <span className={styles.piiSafe}>
                        <Icon name="lock" />
                        {piiText}
                      </span>
                    </>
                  ) : null}
                </ListBoxItem>
              );
            }}
          </ListBox>
        </div>
      ) : null}

      <PurposeSelector value={purpose} onChange={onPurposeChange} compact disabled={disabled} />
      <div className={styles.settings}>{settings}</div>
    </div>
  );
}
