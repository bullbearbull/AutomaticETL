import type { ReactNode } from 'react';
import { workspaceCopy } from '../../copy/common.ts';
import { Details } from '../Details/Details.tsx';
import { useMediaQuery } from './useMediaQuery.ts';
import styles from './Workspace.module.css';

export interface WorkspaceProps {
  /** Left column: the file list (and a compact FileDropZone). */
  list: ReactNode;
  /** Centre column: the result view. */
  result: ReactNode;
  /** Right column: how the result was made (근거). Optional. */
  evidence?: ReactNode;
  /** Under the result: what will be exported and the save buttons. Optional. */
  exportBar?: ReactNode;
  /** The bottom line, normally a StatusBar. */
  status: ReactNode;
  /** Accessible names of the three regions. Default 목록 · 결과 · 근거. */
  labels?: { list?: string; result?: string; evidence?: string };
  /** Toggle text for the evidence panel on medium screens. Default "근거 보기". */
  evidenceToggleLabel?: string;
}

/**
 * The working layout every tab shares: 목록 | 결과 | 근거 with an export bar
 * and a status bar at the bottom.
 *
 * - 1200px and wider: three columns (260px · rest · 320px).
 * - 768–1199px: two columns; 근거 becomes a folded panel above the result.
 * - 767px and narrower (also 400% zoom): one column in reading order
 *   목록 → 결과 → 근거 → 내보내기, then the status bar.
 *
 * Breakpoints are in em, so they follow the browser's font size and zoom.
 * On medium screens 근거 comes before 결과 in the DOM as well, so the reading
 * and focus order match the screen. The cost: when a resize or zoom crosses
 * into or out of medium, React mounts 근거 again, and state inside it (an
 * open 자세히, for example) and focus in it start over.
 */
export function Workspace({
  list,
  result,
  evidence,
  exportBar,
  status,
  labels = {},
  evidenceToggleLabel = workspaceCopy.showEvidence,
}: WorkspaceProps) {
  const wide = useMediaQuery('(min-width: 75em)');
  const medium = useMediaQuery('(min-width: 48em)') && !wide;
  const layout = wide ? 'wide' : medium ? 'medium' : 'narrow';
  const hasEvidence = evidence !== undefined && evidence !== null && evidence !== false;

  const evidenceSection = hasEvidence ? (
    <section className={styles.evidence} aria-label={labels.evidence ?? workspaceCopy.evidence}>
      {layout === 'medium' ? <Details summary={evidenceToggleLabel}>{evidence}</Details> : evidence}
    </section>
  ) : null;

  return (
    <div className={`${styles.workspace} ${styles[layout]}`} data-layout={layout}>
      <section className={styles.list} aria-label={labels.list ?? workspaceCopy.list}>
        {list}
      </section>
      {/* On medium screens the folded 근거 comes first in the DOM too, so the
          reading and focus order match what is shown. */}
      {layout === 'medium' ? evidenceSection : null}
      <section className={styles.result} aria-label={labels.result ?? workspaceCopy.result}>
        {result}
      </section>
      {layout !== 'medium' ? evidenceSection : null}
      {exportBar ? <div className={styles.exportBar}>{exportBar}</div> : null}
      <div className={styles.status}>{status}</div>
    </div>
  );
}
