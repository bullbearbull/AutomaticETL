import { useEffect, useState, type Key, type ReactNode } from 'react';
import { Tab, TabList, TabPanel, Tabs } from 'react-aria-components';
import styles from './AppTabs.module.css';

export interface AppTabItem<K extends string = string> {
  id: K;
  label: string;
  /**
   * The panel content. It is rendered the first time its tab is selected
   * and then stays mounted, hidden while another tab is selected.
   */
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
 *
 * A tab that has been opened keeps its panel mounted (hidden and inert
 * while another tab is selected), so reading in progress and results are
 * still there after the user looks at another tab and comes back.
 */
export function AppTabs<K extends string>({
  label,
  items,
  selectedId,
  defaultSelectedId,
  onSelectionChange,
}: AppTabsProps<K>) {
  const [visited, setVisited] = useState<ReadonlySet<string>>(() => {
    const first = selectedId ?? defaultSelectedId ?? items[0]?.id;
    return new Set(first === undefined ? [] : [first]);
  });

  const markVisited = (id: string) => {
    setVisited((previous) => (previous.has(id) ? previous : new Set([...previous, id])));
  };

  // A selection set by the parent (not by a click) also counts as a visit.
  useEffect(() => {
    if (selectedId === undefined) return;
    setVisited((previous) => (previous.has(selectedId) ? previous : new Set([...previous, selectedId])));
  }, [selectedId]);

  return (
    <Tabs
      className={styles.tabs}
      {...(selectedId !== undefined ? { selectedKey: selectedId } : {})}
      {...(defaultSelectedId !== undefined ? { defaultSelectedKey: defaultSelectedId } : {})}
      onSelectionChange={(key: Key) => {
        const id = String(key) as K;
        markVisited(id);
        onSelectionChange?.(id);
      }}
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
        <TabPanel key={item.id} id={item.id} className={styles.panel} shouldForceMount={visited.has(item.id)}>
          {item.panel}
        </TabPanel>
      ))}
    </Tabs>
  );
}
