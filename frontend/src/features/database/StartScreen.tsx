import type { ReactNode, Ref } from 'react';
import { FileDropZone } from '../../components/index.ts';
import type { Purpose } from './api.ts';
import { startCopy } from './copy.ts';
import { PurposeSelector } from './PurposeSelector.tsx';
import styles from './StartScreen.module.css';

export interface StartScreenProps {
  headingRef: Ref<HTMLHeadingElement>;
  onFiles: (files: File[]) => void;
  purpose: Purpose;
  onPurposeChange: (purpose: Purpose) => void;
  settings: ReactNode;
}

/**
 * Before any file is added: drop zone, DB purpose selector (with immediate
 * `원본 보관용` guidance), and folded 세부 설정. Analysis starts immediately
 * when files arrive.
 */
export function StartScreen({ headingRef, onFiles, purpose, onPurposeChange, settings }: StartScreenProps) {
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
        <PurposeSelector value={purpose} onChange={onPurposeChange} />
        <div className={styles.settings}>{settings}</div>
      </div>
    </div>
  );
}
