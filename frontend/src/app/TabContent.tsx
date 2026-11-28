import { Component, lazy, Suspense, useState, type ComponentType, type ReactNode } from 'react';
import { tabErrorCopy } from '../copy/common.ts';
import { Button, ErrorNotice } from '../components/index.ts';

type TabModule = { default: ComponentType };

/** One tab's entry module and the name of its files in the build. */
export interface TabSource {
  /** For example `() => import('…/ExtractTab.tsx')`. */
  load: () => Promise<TabModule>;
  /** The build writes the tab to assets/<file>.js and assets/<file>.css, for example `ExtractTab`. */
  file: string;
}

interface TabErrorBoundaryProps {
  onRetry: () => void;
  children: ReactNode;
}

interface TabErrorBoundaryState {
  error: Error | null;
}

/**
 * Catches a tab's failed load or render error, so only that panel shows a
 * notice. Without it React would unmount the whole app, and the other tabs
 * would lose their results.
 */
class TabErrorBoundary extends Component<TabErrorBoundaryProps, TabErrorBoundaryState> {
  override state: TabErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: unknown): TabErrorBoundaryState {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  override render() {
    const { error } = this.state;
    if (error === null) return this.props.children;
    return (
      <ErrorNotice
        error={{ ...tabErrorCopy.notice, detail: error.message }}
        actions={
          <Button variant="primary" onPress={this.props.onRetry}>
            {tabErrorCopy.retry}
          </Button>
        }
      />
    );
  }
}

/**
 * Asks for a tab's files again after a failure. Browsers without retriable
 * module loading (Safari, Chrome before 156, Firefox before 155) answer a
 * module URL that failed with the same failure for as long as the page is
 * open, and Vite's loader does not ask for a stylesheet a second time. So
 * both files are asked for under a new URL (`?retry=<n>`; the server ignores
 * the query). In development the files have other names, so the loader just
 * runs again.
 */
function loadAgain({ load, file }: TabSource, attempt: number): Promise<TabModule> {
  if (import.meta.env.DEV) return load();
  const query = `?retry=${attempt}`;
  // In the build this code is in assets/index.js, next to the tab files.
  const assets = import.meta.url;
  return reloadFailedStylesheet(new URL(`${file}.css`, assets).href, query).then(
    () => import(/* @vite-ignore */ new URL(`${file}.js${query}`, assets).href) as Promise<TabModule>,
  );
}

/**
 * Whether a stylesheet link did not load. After a failed load Chrome keeps a
 * sheet whose rules cannot be read (reading them throws), and other browsers
 * may keep no sheet. An empty sheet counts too, because Vite writes a tab
 * stylesheet only when the tab has styles.
 */
function didNotLoad(link: HTMLLinkElement): boolean {
  try {
    return (link.sheet?.cssRules.length ?? 0) === 0;
  } catch {
    return true;
  }
}

/** If the stylesheet at `href` did not load, adds it again under a new URL and waits for it. */
function reloadFailedStylesheet(href: string, query: string): Promise<void> {
  const failed = Array.from(document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"]')).filter(
    (link) => link.href === href && didNotLoad(link),
  );
  if (failed.length === 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = `${href}${query}`;
    link.addEventListener(
      'load',
      () => {
        for (const old of failed) old.remove();
        resolve();
      },
      { once: true },
    );
    link.addEventListener(
      'error',
      () => {
        link.remove();
        reject(new Error(`Unable to load CSS from ${link.href}`));
      },
      { once: true },
    );
    document.head.append(link);
  });
}

export interface TabContentProps {
  source: TabSource;
  /** Shown while the tab's file is loading. */
  fallback: ReactNode;
}

/**
 * One tab's screen, loaded the first time the tab opens. A lazy component
 * that failed stays failed in React, so [다시 불러오기] makes a new one, which
 * asks for the files again, inside a new boundary.
 */
export function TabContent({ source, fallback }: TabContentProps) {
  const [attempt, setAttempt] = useState(0);
  const [Screen, setScreen] = useState(() => lazy(source.load));

  const retry = () => {
    const next = attempt + 1;
    setScreen(() => lazy(() => loadAgain(source, next)));
    setAttempt(next);
  };

  return (
    <TabErrorBoundary key={attempt} onRetry={retry}>
      <Suspense fallback={fallback}>
        <Screen />
      </Suspense>
    </TabErrorBoundary>
  );
}
