import { useEffect, useState } from 'react';
import { statusCopy } from '../../copy/common.ts';
import { Icon, type IconName } from '../Icon/Icon.tsx';
import { VisuallyHidden } from '../VisuallyHidden/VisuallyHidden.tsx';
import styles from './StatusBar.module.css';

export type StatusTone = 'idle' | 'busy' | 'done' | 'warning' | 'error';

export interface StatusBarProps {
  /**
   * What is happening, in words: "\"보고서.pdf\"를 읽는 중이에요 · 파일 2개 중
   * 2번째", "표 3개를 찾았어요". Never use the word "단계" here.
   */
  message: string;
  /** Sets the icon and colour. Defaults to `idle` (no icon). */
  tone?: StatusTone;
  /** `Date.now()` when the work started. Shows elapsed seconds when set. */
  startedAt?: number | null;
  /** `Date.now()` when the work ended. Freezes the elapsed time. */
  endedAt?: number | null;
}

const toneIcon: Partial<Record<StatusTone, IconName>> = {
  busy: 'spinner',
  done: 'check',
  warning: 'warning',
  error: 'error',
};

const toneWord: Partial<Record<StatusTone, string>> = {
  done: statusCopy.done,
  warning: statusCopy.warning,
  error: statusCopy.error,
};

/**
 * The progress line at the bottom of a tab. The message sits in a
 * `role="status"` live region, so screen readers read each new message.
 * The elapsed seconds sit outside it, so the ticking clock is not read out
 * every second.
 */
export function StatusBar({ message, tone = 'idle', startedAt = null, endedAt = null }: StatusBarProps) {
  const running = startedAt !== null && endedAt === null;
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!running) return undefined;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, startedAt]);

  const elapsed =
    startedAt === null ? null : Math.max(0, Math.floor(((endedAt ?? now) - startedAt) / 1000));
  const icon = toneIcon[tone];
  const word = toneWord[tone];

  return (
    <div className={`${styles.bar} ${styles[tone]}`}>
      <div role="status" className={styles.message}>
        {icon ? <Icon name={icon} className={styles.icon} /> : null}
        {word ? <VisuallyHidden>{`${word}: `}</VisuallyHidden> : null}
        <span>{message}</span>
      </div>
      {elapsed !== null ? (
        <span className={`${styles.elapsed} tabular-nums`}>
          <span aria-hidden="true">· </span>
          {statusCopy.elapsed(elapsed)}
        </span>
      ) : null}
    </div>
  );
}
