import { Component, lazy, Suspense, useState, type ComponentType, type ReactNode } from 'react';
import { tabErrorCopy } from '../copy/common.ts';
import { Button, ErrorNotice } from '../components/index.ts';

export type TabLoader = () => Promise<{ default: ComponentType }>;

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

export interface TabContentProps {
  /** The tab's entry module, for example `() => import('…/ExtractTab.tsx')`. */
  load: TabLoader;
  /** Shown while the tab's file is loading. */
  fallback: ReactNode;
}

/**
 * One tab's screen, loaded the first time the tab opens. React keeps a lazy
 * component that failed failed, so [다시 불러오기] makes a new one and a new
 * boundary.
 */
export function TabContent({ load, fallback }: TabContentProps) {
  const [attempt, setAttempt] = useState(0);
  const [Screen, setScreen] = useState(() => lazy(load));

  const retry = () => {
    setScreen(() => lazy(load));
    setAttempt((count) => count + 1);
  };

  return (
    <TabErrorBoundary key={attempt} onRetry={retry}>
      <Suspense fallback={fallback}>
        <Screen />
      </Suspense>
    </TabErrorBoundary>
  );
}
