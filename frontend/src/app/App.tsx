import { lazy, Suspense, useEffect, useState, type ComponentType, type LazyExoticComponent } from 'react';
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

/**
 * A file dropped outside a drop zone would make the browser open it in
 * place of the app, and every result would be lost. Drop zones handle
 * their own events first (React listens on the root element) and cancel
 * them, so only drops nobody took reach the window.
 */
function blockStrayFileDrop(event: DragEvent) {
  if (event.defaultPrevented || !event.dataTransfer) return;
  if (!Array.from(event.dataTransfer.types).includes('Files')) return;
  event.preventDefault();
  if (event.type === 'dragover') event.dataTransfer.dropEffect = 'none';
}

export function App() {
  const [selected, setSelected] = useState<TabId>('extract');

  useEffect(() => {
    window.addEventListener('dragover', blockStrayFileDrop);
    window.addEventListener('drop', blockStrayFileDrop);
    return () => {
      window.removeEventListener('dragover', blockStrayFileDrop);
      window.removeEventListener('drop', blockStrayFileDrop);
    };
  }, []);

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
