import type { ReactNode, Ref } from 'react';
import { FileDropZone } from '../../components/index.ts';
import { startCopy } from './copy.ts';
import styles from './StartScreen.module.css';

export interface StartScreenProps {
  /** Focus lands here when the last file leaves the list. */
  headingRef: Ref<HTMLHeadingElement>;
  onFiles: (files: File[]) => void;
  /** The 세부 설정 panel, shared with the workspace. */
  settings: ReactNode;
}

/** Before any file: only the drop zone and the folded 세부 설정. Reading starts as soon as files arrive. */
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
