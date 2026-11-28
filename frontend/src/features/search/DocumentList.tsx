import { useId, useRef, type ReactNode } from 'react';
import { ListBox, ListBoxItem, useDrop, type DropItem } from 'react-aria-components';
import { Button, FileDropZone } from '../../components/index.ts';
import { formatBytes } from '../../lib/files.ts';
import type { DocIndexBuildResult } from './api.ts';
import { listCopy } from './copy.ts';
import { baseFileName, pageUnitFor } from './formats.ts';
import type { SearchFileItem } from './state.ts';
import styles from './DocumentList.module.css';

export interface DocumentListProps {
  files: readonly SearchFileItem[];
  onFiles: (files: File[]) => void;
  onRemoveFile: (id: string) => void;
  index: DocIndexBuildResult | null;
  docFilterId: string;
  onDocFilterChange: (docId: string) => void;
  settings: ReactNode;
  disabled?: boolean;
}

const ALL_DOCS_KEY = '__all__';

async function filesFrom(items: readonly DropItem[]): Promise<File[]> {
  const files: File[] = [];
  for (const item of items) {
    if (item.kind === 'file') files.push(await item.getFile());
  }
  return files;
}

/**
 * Left column (`목록`) for the search tab:
 * - Uploaded documents with remove buttons and drop target
 * - Search scope filter (`전체 문서` or one specific document)
 * - Folded 세부 설정
 */
export function DocumentList({
  files,
  onFiles,
  onRemoveFile,
  index,
  docFilterId,
  onDocFilterChange,
  settings,
  disabled = false,
}: DocumentListProps) {
  const filesHeadingId = useId();
  const docsHeadingId = useId();
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
  const scopeItems = index
    ? [
        {
          id: ALL_DOCS_KEY,
          name: listCopy.allDocumentsLabel(index.documents.length),
          meta: listCopy.allDocumentsOption(index.documents.length, index.sections.length),
        },
        ...index.documents.map((doc) => {
          const name = baseFileName(doc.sourcePath) || doc.title || doc.docId;
          const unit = pageUnitFor(doc.format || name);
          return {
            id: doc.docId,
            name,
            meta: listCopy.documentMeta(`${doc.pageCount}${unit}`, doc.sectionCount, doc.tableCount),
          };
        }),
      ]
    : [];

  return (
    <div className={styles.root}>
      <div
        {...dropProps}
        ref={dropRef}
        className={styles.drop}
        data-drop-target={isDropTarget || undefined}
      >
        <div className={styles.sectionHeader}>
          <h2 id={filesHeadingId} className={styles.heading}>
            {listCopy.filesHeading(files.length)}
          </h2>
          {files.length > 0 ? (
            <span className={`${styles.totalSize} tabular-nums`}>
              {listCopy.totalSize(formatBytes(totalBytes))}
            </span>
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

      {scopeItems.length > 0 ? (
        <div className={styles.scopeSection}>
          <h2 id={docsHeadingId} className={styles.heading}>
            {listCopy.documentsHeading(index?.documents.length ?? 0)}
          </h2>
          <ListBox
            aria-labelledby={docsHeadingId}
            items={scopeItems}
            selectionMode="single"
            selectionBehavior="replace"
            disallowEmptySelection
            selectedKeys={[docFilterId || ALL_DOCS_KEY]}
            onSelectionChange={(keys) => {
              if (keys === 'all') return;
              const [first] = keys;
              if (first !== undefined) {
                const val = String(first);
                onDocFilterChange(val === ALL_DOCS_KEY ? '' : val);
              }
            }}
            className={styles.docList}
          >
            {(item) => (
              <ListBoxItem
                id={item.id}
                textValue={`${item.name}, ${item.meta}`}
                className={styles.docItem}
              >
                <span className={styles.docName}>{item.name}</span>{' '}
                <span className={`${styles.docMeta} tabular-nums`}>{item.meta}</span>
              </ListBoxItem>
            )}
          </ListBox>
        </div>
      ) : null}

      <div className={styles.settings}>{settings}</div>
    </div>
  );
}
