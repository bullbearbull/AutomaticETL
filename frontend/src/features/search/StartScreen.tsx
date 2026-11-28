import type { ReactNode, Ref } from 'react';
import { FileDropZone } from '../../components/index.ts';
import { startCopy } from './copy.ts';
import styles from './StartScreen.module.css';

export interface StartScreenProps {
  headingRef: Ref<HTMLHeadingElement>;
  onFiles: (files: File[]) => void;
  settings: ReactNode;
}

/**
 * Start view before any document is added: headline, open file drop zone
 * (never filtering extensions in the picker so Task 5 `.xls` files get helpful
 * guidance), and folded search settings.
 */
export function StartScreen({ headingRef, onFiles, settings }: StartScreenProps) {
  return (
    <div className={styles.root}>
      <div className={styles.inner}>
        <header className={styles.header}>
          <h2 ref={headingRef} tabIndex={-1} className={styles.title}>
            {startCopy.title}
          </h2>
          <p className={styles.lead}>{startCopy.lead}</p>
        </header>
        <FileDropZone
          onFiles={onFiles}
          hint={
            <>
              <span className={styles.hintLine}>{startCopy.formats}</span>
              <span className={styles.hintLine}>{startCopy.limits}</span>
            </>
          }
        />
        <div className={styles.settings}>{settings}</div>
      </div>
    </div>
  );
}
