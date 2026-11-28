import { ComingSoon } from '../../components/index.ts';
import { tabLabels } from '../../copy/common.ts';
import styles from './SettingsTab.module.css';

/**
 * Entry of the "settings" tab. App.tsx loads this file lazily, so
 * everything under features/settings/ is built into assets/SettingsTab.js and
 * assets/SettingsTab.css. For now it shows a placeholder that links to the
 * current screen at "/".
 */
export default function SettingsTab() {
  return (
    <div className={styles.root}>
      <ComingSoon title={tabLabels.settings} />
    </div>
  );
}
