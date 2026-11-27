import { useId, useRef, type ReactNode } from 'react';
import { ListBox, ListBoxItem, useDrop, type DropItem } from 'react-aria-components';
import { Button, FileDropZone, Icon } from '../../components/index.ts';
import { listCopy, rereadCopy } from './copy.ts';
import { entryMeta, type ExtractEntry } from './state.ts';
import styles from './FileList.module.css';

export interface FileListProps {
  entries: readonly ExtractEntry[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onFiles: (files: File[]) => void;
  /** The 세부 설정 panel, shared with the start screen. */
  settings: ReactNode;
  /** Set when the shown file was read with other settings than the current ones. */
  reread: { name: string; onReread: () => void } | null;
}

async function filesFrom(items: readonly DropItem[]): Promise<File[]> {
  const files: File[] = [];
  for (const item of items) {
    if (item.kind === 'file') files.push(await item.getFile());
  }
  return files;
}

/**
 * The 목록 column: every file with its state, [+ 파일 넣기], and 세부 설정.
 * Files dropped anywhere on the list are added too. Arrow keys move through
 * the list and show the file under focus.
 */
export function FileList({ entries, selectedId, onSelect, onFiles, settings, reread }: FileListProps) {
  const headingId = useId();
  const dropRef = useRef<HTMLDivElement>(null);
  const { dropProps, isDropTarget } = useDrop({
    ref: dropRef,
    getDropOperation: () => 'copy',
    onDrop: (event) => {
      void filesFrom(event.items).then((files) => {
        if (files.length > 0) onFiles(files);
      });
    },
  });

  return (
    <div className={styles.root}>
      <div {...dropProps} ref={dropRef} className={styles.drop} data-drop-target={isDropTarget || undefined}>
        <h2 id={headingId} className={styles.heading}>
          {listCopy.heading(entries.length)}
        </h2>
        <ListBox
          aria-labelledby={headingId}
          items={entries}
          selectionMode="single"
          selectionBehavior="replace"
          disallowEmptySelection
          selectedKeys={selectedId ? [selectedId] : []}
          onSelectionChange={(keys) => {
            if (keys === 'all') return;
            const [first] = keys;
            if (first !== undefined) onSelect(String(first));
          }}
          className={styles.list}
        >
          {(entry) => (
            <ListBoxItem
              id={entry.id}
              textValue={entry.name}
              aria-label={listCopy.option(entry.name, entryMeta(entry))}
              className={styles.item}
            >
              <span className={`${styles.icon} ${styles[entry.status]}`}>
                <EntryIcon status={entry.status} />
              </span>
              <span className={styles.text}>
                {/* The space keeps name and note apart in the text, so the visible text stays inside the label. */}
                <span className={styles.name}>{entry.name}</span>{' '}
                <span className={`${styles.meta} ${entry.status === 'error' ? styles.metaError : ''}`}>
                  {entryMeta(entry)}
                </span>
              </span>
            </ListBoxItem>
          )}
        </ListBox>
        <FileDropZone variant="compact" onFiles={onFiles} />
      </div>

      <div className={styles.settings}>{settings}</div>

      <div className={styles.reread}>
        {/* Always mounted, so a new message is read out. */}
        <p role="status" className={styles.rereadText}>
          {reread ? rereadCopy.changed(reread.name) : null}
        </p>
        {reread ? <Button onPress={reread.onReread}>{rereadCopy.button}</Button> : null}
      </div>
    </div>
  );
}

function EntryIcon({ status }: { status: ExtractEntry['status'] }) {
  switch (status) {
    case 'done':
      return <Icon name="check" />;
    case 'error':
      return <Icon name="error" />;
    case 'reading':
      return <Icon name="spinner" />;
    default:
      return null;
  }
}
