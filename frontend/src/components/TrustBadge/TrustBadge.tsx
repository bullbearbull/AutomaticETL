import { appCopy } from '../../copy/common.ts';
import { Icon } from '../Icon/Icon.tsx';
import styles from './TrustBadge.module.css';

/** "내 PC에서만 실행 · 외부 전송 없음", always visible in the top bar. */
export function TrustBadge() {
  return (
    <p className={styles.badge}>
      <Icon name="lock" />
      <span>{appCopy.trust}</span>
    </p>
  );
}
