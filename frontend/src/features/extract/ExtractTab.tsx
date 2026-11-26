import { ComingSoon } from '../../components/index.ts';
import { tabLabels } from '../../copy/common.ts';
import styles from './ExtractTab.module.css';

/**
 * Entry of the "extract" tab. App.tsx loads this file lazily, so
 * everything under features/extract/ is built into assets/ExtractTab.js and
 * assets/ExtractTab.css. For now it shows a placeholder that links to the
 * current screen at "/".
 */
export default function ExtractTab() {
  return (
    <div className={styles.root}>
      <ComingSoon title={tabLabels.extract} />
    </div>
  );
}
