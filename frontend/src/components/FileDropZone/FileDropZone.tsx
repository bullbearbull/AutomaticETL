import type { ReactNode } from 'react';
import { DropZone, FileTrigger, Text, type DropItem } from 'react-aria-components';
import { buttonCopy, dropZoneCopy } from '../../copy/common.ts';
import { Button } from '../Button/Button.tsx';
import styles from './FileDropZone.module.css';

export interface FileDropZoneProps {
  /** Called with the dropped or picked files. Check extensions and size here. */
  onFiles: (files: File[]) => void;
  /** Allow several files at once. Defaults to true. */
  allowsMultiple?: boolean;
  /**
   * `large`: the start screen's dashed box with a primary [파일 고르기].
   * `compact`: a [+ 파일 넣기] button for the list column.
   */
  variant?: 'large' | 'compact';
  /** Text above the button (large) — defaults to "여기에 파일을 끌어 놓거나". */
  prompt?: ReactNode;
  /** Supported formats and size, shown under the button. */
  hint?: ReactNode;
  /** Button text; defaults to "파일 고르기" (large) or "+ 파일 넣기" (compact). */
  buttonLabel?: string;
  isDisabled?: boolean;
}

/**
 * Drag-and-drop plus a file picker button, so files can be added without a
 * mouse. The picker never filters by type (no `accept`): a file the tab
 * cannot read is explained after it is added.
 */
export function FileDropZone({
  onFiles,
  allowsMultiple = true,
  variant = 'large',
  prompt = dropZoneCopy.prompt,
  hint,
  buttonLabel,
  isDisabled = false,
}: FileDropZoneProps) {
  const handleDrop = async (event: { items: readonly DropItem[] }) => {
    const files: File[] = [];
    for (const item of event.items) {
      if (item.kind === 'file') files.push(await item.getFile());
    }
    const accepted = allowsMultiple ? files : files.slice(0, 1);
    if (accepted.length > 0) onFiles(accepted);
  };

  const handleSelect = (list: FileList | null) => {
    const files = list ? Array.from(list) : [];
    if (files.length > 0) onFiles(allowsMultiple ? files : files.slice(0, 1));
  };

  const compact = variant === 'compact';
  const label = buttonLabel ?? (compact ? buttonCopy.addFiles : buttonCopy.pickFiles);

  return (
    <DropZone
      className={`${styles.zone} ${compact ? styles.compact : styles.large}`}
      getDropOperation={() => 'copy'}
      onDrop={(event) => void handleDrop(event)}
      isDisabled={isDisabled}
    >
      <Text slot="label" className={compact ? styles.srLabel : styles.prompt}>
        {compact ? dropZoneCopy.label : prompt}
      </Text>
      <FileTrigger allowsMultiple={allowsMultiple} onSelect={handleSelect}>
        <Button variant={compact ? 'secondary' : 'primary'} isDisabled={isDisabled} className={styles.button}>
          {label}
        </Button>
      </FileTrigger>
      <div className={styles.hint}>{hint ?? (compact ? dropZoneCopy.compactHint : null)}</div>
    </DropZone>
  );
}
