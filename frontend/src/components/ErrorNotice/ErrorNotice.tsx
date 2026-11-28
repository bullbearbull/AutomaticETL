import type { ReactNode } from 'react';
import { errorTemplateCopy, statusCopy } from '../../copy/common.ts';
import { Details } from '../Details/Details.tsx';
import { Icon } from '../Icon/Icon.tsx';
import { VisuallyHidden } from '../VisuallyHidden/VisuallyHidden.tsx';
import styles from './ErrorNotice.module.css';

/** What ErrorNotice shows. An `AppError` from lib/errors.ts fits as is. */
export interface ErrorNoticeContent {
  /** 무슨 일: the title. */
  what: string;
  /** 왜: the reason. */
  why: string;
  /** 어떻게: what to do next. */
  how: string;
  /** The raw server or browser text, folded under 자세히. */
  detail?: string | undefined;
}

export interface ErrorNoticeProps {
  error: ErrorNoticeContent;
  /** Next-step buttons, for example [다른 파일 넣기]. */
  actions?: ReactNode;
  className?: string;
}

/**
 * An error in the 무슨 일 · 왜 · 어떻게 template, announced through
 * `role="alert"`. Colour, an icon and text always appear together.
 */
export function ErrorNotice({ error, actions, className }: ErrorNoticeProps) {
  return (
    <div role="alert" className={[styles.notice, className ?? ''].join(' ').trim()}>
      <p className={styles.title}>
        <Icon name="error" className={styles.icon} />
        <VisuallyHidden>{`${statusCopy.error}: `}</VisuallyHidden>
        {error.what}
      </p>
      <dl className={styles.parts}>
        <div>
          <dt>{errorTemplateCopy.why}</dt>
          <dd>{error.why}</dd>
        </div>
        <div>
          <dt>{errorTemplateCopy.how}</dt>
          <dd>{error.how}</dd>
        </div>
      </dl>
      {actions ? <div className={styles.actions}>{actions}</div> : null}
      {error.detail ? (
        <Details summary={errorTemplateCopy.rawDetail} className={styles.detail}>
          <pre className={styles.raw}>{error.detail}</pre>
        </Details>
      ) : null}
    </div>
  );
}
