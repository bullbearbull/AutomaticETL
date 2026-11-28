import { Button as AriaButton, type ButtonProps as AriaButtonProps } from 'react-aria-components';
import styles from './Button.module.css';

export interface ButtonProps extends Omit<AriaButtonProps, 'className'> {
  /**
   * `primary`: black, one per screen, 40px high. `secondary` (default):
   * outlined, 36px. `quiet`: text-like, for small actions such as 자세히.
   */
  variant?: 'primary' | 'secondary' | 'quiet';
  className?: string;
}

/**
 * A React Aria button. Label it "무엇을 + 하기" (for example
 * "ZIP으로 저장하기"). Use `onPress`, not `onClick`.
 */
export function Button({ variant = 'secondary', className, ...props }: ButtonProps) {
  return <AriaButton {...props} className={[styles.button, styles[variant], className ?? ''].join(' ').trim()} />;
}
