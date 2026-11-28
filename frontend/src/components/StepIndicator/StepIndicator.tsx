import { stepCopy, stepLabels } from '../../copy/common.ts';
import { Icon } from '../Icon/Icon.tsx';
import { VisuallyHidden } from '../VisuallyHidden/VisuallyHidden.tsx';
import styles from './StepIndicator.module.css';

export type StepNumber = 1 | 2 | 3;

export interface StepIndicatorProps {
  /** The step the user is on: 1 넣기, 2 확인·다듬기, 3 내보내기. */
  current: StepNumber;
  /** Defaults to the shared three steps; tabs keep the same names. */
  labels?: readonly [string, string, string];
}

/**
 * Where the user is in 넣기 → 확인·다듬기 → 내보내기. It is a list, not
 * buttons; the current step has `aria-current="step"`.
 */
export function StepIndicator({ current, labels = stepLabels }: StepIndicatorProps) {
  return (
    <ol className={styles.list} aria-label={stepCopy.listLabel}>
      {labels.map((label, index) => {
        const number = (index + 1) as StepNumber;
        const state = number < current ? 'done' : number === current ? 'current' : 'todo';
        return (
          <li key={label} className={`${styles.step} ${styles[state]}`} aria-current={state === 'current' ? 'step' : undefined}>
            <span className={styles.marker} aria-hidden="true">
              {state === 'done' ? <Icon name="check" /> : number}
            </span>
            <span className={styles.label}>
              <VisuallyHidden>{`${number} `}</VisuallyHidden>
              {label}
              {state === 'done' ? <VisuallyHidden>{` (${stepCopy.done})`}</VisuallyHidden> : null}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
