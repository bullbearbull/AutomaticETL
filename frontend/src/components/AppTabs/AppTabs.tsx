import type { Key, ReactNode } from 'react';
import { Tab, TabList, TabPanel, Tabs } from 'react-aria-components';
import styles from './AppTabs.module.css';

export interface AppTabItem<K extends string = string> {
  id: K;
  label: string;
  /** The panel content. Only the selected panel is rendered. */
  panel: ReactNode;
  /** `end` pushes the tab to the right end of the bar (used by 설정). */
  placement?: 'start' | 'end';
}

export interface AppTabsProps<K extends string = string> {
  /** Accessible name of the tab list. */
  label: string;
  items: readonly AppTabItem<K>[];
  selectedId?: K;
  defaultSelectedId?: K;
  onSelectionChange?: (id: K) => void;
}

/**
 * Standard tabs (tablist · tab · tabpanel). Arrow keys move between tabs,
 * Home/End jump to the ends, and the selected tab shows bold text with an
 * underline, so it never depends on colour.
 */
export function AppTabs<K extends string>({
  label,
  items,
  selectedId,
  defaultSelectedId,
  onSelectionChange,
}: AppTabsProps<K>) {
  return (
    <Tabs
      className={styles.tabs}
      {...(selectedId !== undefined ? { selectedKey: selectedId } : {})}
      {...(defaultSelectedId !== undefined ? { defaultSelectedKey: defaultSelectedId } : {})}
      onSelectionChange={(key: Key) => onSelectionChange?.(String(key) as K)}
    >
      <div className={styles.bar}>
        <TabList aria-label={label} className={styles.list}>
          {items.map((item) => (
            <Tab key={item.id} id={item.id} className={item.placement === 'end' ? `${styles.tab} ${styles.end}` : styles.tab}>
              {item.label}
            </Tab>
          ))}
        </TabList>
      </div>
      {items.map((item) => (
        <TabPanel key={item.id} id={item.id} className={styles.panel}>
          {item.panel}
        </TabPanel>
      ))}
    </Tabs>
  );
}
