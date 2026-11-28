import type { ReactNode } from 'react';
import { Button, Disclosure, DisclosurePanel } from 'react-aria-components';
import { buttonCopy } from '../../copy/common.ts';
import styles from './Details.module.css';

export interface DetailsProps {
  /** The toggle text. Defaults to "자세히". */
  summary?: ReactNode;
  children: ReactNode;
  defaultExpanded?: boolean;
  /** Controlled open state; pair with `onExpandedChange`. */
  isExpanded?: boolean;
  onExpandedChange?: (isExpanded: boolean) => void;
  className?: string;
}

/**
 * A folded section (React Aria Disclosure): technical detail, raw server
 * text and optional settings go here so the main screen stays plain.
 */
export function Details({
  summary = buttonCopy.details,
  children,
  defaultExpanded,
  isExpanded,
  onExpandedChange,
  className,
}: DetailsProps) {
  return (
    <Disclosure
      className={[styles.details, className ?? ''].join(' ').trim()}
      {...(defaultExpanded !== undefined ? { defaultExpanded } : {})}
      {...(isExpanded !== undefined ? { isExpanded } : {})}
      {...(onExpandedChange ? { onExpandedChange } : {})}
    >
      <Button slot="trigger" className={styles.trigger}>
        <span className={styles.chevron} aria-hidden="true" />
        {summary}
      </Button>
      <DisclosurePanel className={styles.panel}>{children}</DisclosurePanel>
    </Disclosure>
  );
}
