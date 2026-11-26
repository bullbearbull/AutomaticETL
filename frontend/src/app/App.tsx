import { lazy, Suspense, useState, type ComponentType, type LazyExoticComponent } from 'react';
import { appCopy, tabIds, tabLabels, type TabId } from '../copy/common.ts';
import { AppTabs, StatusBar, TrustBadge, type AppTabItem } from '../components/index.ts';
import styles from './App.module.css';

/**
 * Each tab is a lazy entry with a unique file name, so the build writes its
 * code to its own assets/<Name>Tab.js and .css. Shared code stays in index.
 */
const tabComponents: Record<TabId, LazyExoticComponent<ComponentType>> = {
  extract: lazy(() => import('../features/extract/ExtractTab.tsx')),
  database: lazy(() => import('../features/database/DatabaseTab.tsx')),
  search: lazy(() => import('../features/search/SearchTab.tsx')),
  settings: lazy(() => import('../features/settings/SettingsTab.tsx')),
};

function TabLoading() {
  return <StatusBar message={appCopy.loadingTab} tone="busy" />;
}

export function App() {
  const [selected, setSelected] = useState<TabId>('extract');

  const items: AppTabItem<TabId>[] = tabIds.map((id) => {
    const TabComponent = tabComponents[id];
    return {
      id,
      label: tabLabels[id],
      placement: id === 'settings' ? 'end' : 'start',
      panel: (
        <Suspense fallback={<TabLoading />}>
          <TabComponent />
        </Suspense>
      ),
    };
  });

  return (
    <div className={styles.shell}>
      <header className={styles.topBar}>
        <h1 className={styles.appName}>{appCopy.name}</h1>
        <TrustBadge />
      </header>
      <main className={styles.main}>
        <AppTabs label={appCopy.tabListLabel} items={items} selectedId={selected} onSelectionChange={setSelected} />
      </main>
    </div>
  );
}
