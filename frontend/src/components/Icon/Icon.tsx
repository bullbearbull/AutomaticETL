import type { ReactNode } from 'react';
import styles from './Icon.module.css';

export type IconName = 'check' | 'error' | 'warning' | 'info' | 'spinner' | 'lock' | 'external';

export interface IconProps {
  /** Which icon to draw. Icons are decorative: always pair them with text. */
  name: IconName;
  /** Optional extra class name. */
  className?: string;
}

/** A 1em inline SVG icon, hidden from screen readers. */
export function Icon({ name, className }: IconProps) {
  const classes = [styles.icon, name === 'spinner' ? styles.spinner : '', className ?? ''].filter(Boolean).join(' ');
  return (
    <svg className={classes} viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      {paths[name]}
    </svg>
  );
}

const stroke = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.75,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
} as const;

const paths: Record<IconName, ReactNode> = {
  check: <path {...stroke} d="M3 8.5l3.2 3L13 4.5" />,
  error: (
    <>
      <circle {...stroke} cx="8" cy="8" r="6.25" />
      <path {...stroke} d="M8 4.75v3.75M8 11.25v.01" />
    </>
  ),
  warning: (
    <>
      <path {...stroke} d="M8 1.9l6.4 11.35H1.6z" />
      <path {...stroke} d="M8 6.25v3M8 11.4v.01" />
    </>
  ),
  info: (
    <>
      <circle {...stroke} cx="8" cy="8" r="6.25" />
      <path {...stroke} d="M8 7.25v4M8 4.75v.01" />
    </>
  ),
  spinner: (
    <>
      <circle {...stroke} cx="8" cy="8" r="6" opacity="0.25" />
      <path {...stroke} d="M8 2a6 6 0 0 1 6 6" />
    </>
  ),
  lock: (
    <>
      <rect {...stroke} x="3" y="7" width="10" height="7" rx="1.5" />
      <path {...stroke} d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" />
    </>
  ),
  external: <path {...stroke} d="M9.5 2.5h4v4M13.5 2.5L7.5 8.5M12 9.5v3a1 1 0 0 1-1 1H3.5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h3" />,
};
