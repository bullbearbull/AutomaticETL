import { ComingSoon } from '../../components/index.ts';
import { tabLabels } from '../../copy/common.ts';
import styles from './SearchTab.module.css';

/**
 * Entry of the "search" tab. App.tsx loads this file lazily, so
 * everything under features/search/ is built into assets/SearchTab.js and
 * assets/SearchTab.css. For now it shows a placeholder that links to the
 * current screen at "/".
 */
export default function SearchTab() {
  return (
    <div className={styles.root}>
      <ComingSoon title={tabLabels.search} />
    </div>
  );
}
