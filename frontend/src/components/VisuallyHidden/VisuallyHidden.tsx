import type { ReactNode } from 'react';
import styles from './VisuallyHidden.module.css';

export interface VisuallyHiddenProps {
  /** Text for screen readers only. */
  children: ReactNode;
}

/** Hides content visually but keeps it for screen readers. */
export function VisuallyHidden({ children }: VisuallyHiddenProps) {
  return <span className={styles.root}>{children}</span>;
}
