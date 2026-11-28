import { comingSoonCopy } from '../../copy/common.ts';
import { Icon } from '../Icon/Icon.tsx';
import styles from './ComingSoon.module.css';

export interface ComingSoonProps {
  /** The tab name, shown as the panel heading. */
  title: string;
  /** Optional line about what the tab will do. */
  description?: string;
  /** Where the current screen lives. Defaults to "/". */
  href?: string;
}

/** A "준비 중" panel that links to the current screen. */
export function ComingSoon({ title, description, href = '/' }: ComingSoonProps) {
  return (
    <div className={styles.root}>
      <h2 className={styles.title}>{title}</h2>
      {description ? <p>{description}</p> : null}
      <p className={styles.body}>
        <Icon name="info" />
        <span>{comingSoonCopy.body}</span>
      </p>
      <p>
        <a href={href} className={styles.link}>
          {comingSoonCopy.link}
        </a>
      </p>
    </div>
  );
}
