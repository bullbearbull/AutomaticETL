import { ComingSoon } from '../../components/index.ts';
import { tabLabels } from '../../copy/common.ts';
import styles from './DatabaseTab.module.css';

/**
 * Entry of the "database" tab. App.tsx loads this file lazily, so
 * everything under features/database/ is built into assets/DatabaseTab.js and
 * assets/DatabaseTab.css. For now it shows a placeholder that links to the
 * current screen at "/".
 */
export default function DatabaseTab() {
  return (
    <div className={styles.root}>
      <ComingSoon title={tabLabels.database} />
    </div>
  );
}
