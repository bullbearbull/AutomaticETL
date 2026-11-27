import type { Key, Ref } from 'react';
import { FileTrigger, Tab, TabList, TabPanel, Tabs } from 'react-aria-components';
import { Button, ErrorNotice, Icon } from '../../components/index.ts';
import { formatBytes } from '../../lib/files.ts';
import type { BodyModel } from './body.ts';
import { BodyView } from './BodyView.tsx';
import { ChunksView } from './ChunksView.tsx';
import { extractErrorCopy, resultCopy } from './copy.ts';
import { errorAction } from './errors.ts';
import { formatFromName, formatName, unitForFormat } from './formats.ts';
import type { ExtractEntry } from './state.ts';
import { TablesView } from './TablesView.tsx';
import { VisualsView } from './VisualFigure.tsx';
import styles from './ResultView.module.css';

export const RESULT_VIEWS = ['body', 'tables', 'visuals', 'chunks', 'markdown'] as const;
export type ResultViewId = (typeof RESULT_VIEWS)[number];

function isResultView(key: Key): key is ResultViewId {
  return (RESULT_VIEWS as readonly Key[]).includes(key);
}

export interface ResultViewProps {
  entry: ExtractEntry;
  /** The parsed body of `entry.result`, built once by the tab. */
  body: BodyModel | null;
  /** The inner view, kept by the tab so it stays the same from file to file. */
  view: ResultViewId;
  onViewChange: (view: ResultViewId) => void;
  /** The file name heading; focus lands here when the shown file changes under the user's hands. */
  headingRef: Ref<HTMLHeadingElement>;
  onPickOther: (files: File[]) => void;
  onRemove: () => void;
  onRetry: () => void;
  onExported: () => void;
}

/** The 결과 column: the shown file's name, then its result, progress or error. */
export function ResultView({
  entry,
  body,
  view,
  onViewChange,
  headingRef,
  onPickOther,
  onRemove,
  onRetry,
  onExported,
}: ResultViewProps) {
  const format = entry.result?.document.format || formatFromName(entry.name);
  return (
    <div className={styles.root}>
      <header className={styles.header}>
        <h2 ref={headingRef} tabIndex={-1} className={styles.title}>
          {entry.name}
        </h2>
        <p className={styles.meta}>{resultCopy.meta(formatName(format), formatBytes(entry.size))}</p>
      </header>

      {entry.status === 'queued' ? <p className={styles.waiting}>{resultCopy.queued}</p> : null}

      {entry.status === 'reading' ? (
        <p className={styles.waiting}>
          <Icon name="spinner" />
          {resultCopy.reading}
        </p>
      ) : null}

      {entry.status === 'error' && entry.error ? (
        <ErrorNotice
          error={entry.error}
          actions={
            <>
              {errorAction(entry.error.kind) === 'pickOther' ? (
                <FileTrigger
                  allowsMultiple
                  onSelect={(list) => {
                    const files = list ? Array.from(list) : [];
                    if (files.length > 0) onPickOther(files);
                  }}
                >
                  <Button variant="primary">{extractErrorCopy.actions.pickOther}</Button>
                </FileTrigger>
              ) : (
                <Button variant="primary" onPress={onRetry}>
                  {extractErrorCopy.actions.retry}
                </Button>
              )}
              <Button onPress={onRemove}>{extractErrorCopy.actions.remove}</Button>
            </>
          }
        />
      ) : null}

      {entry.status === 'done' && entry.result && body ? (
        <Tabs
          selectedKey={view}
          onSelectionChange={(key) => {
            if (isResultView(key)) onViewChange(key);
          }}
          className={styles.tabs}
        >
          <TabList aria-label={resultCopy.viewsLabel} className={styles.tabList}>
            <Tab id="body" className={styles.tab}>
              {resultCopy.body}
            </Tab>
            <Tab id="tables" className={styles.tab}>
              {resultCopy.tables(entry.result.tables.length)}
            </Tab>
            <Tab id="visuals" className={styles.tab}>
              {resultCopy.visuals(entry.result.visuals.length)}
            </Tab>
            <Tab id="chunks" className={styles.tab}>
              {resultCopy.chunks}
            </Tab>
            <Tab id="markdown" className={`${styles.tab} ${styles.tabEnd}`}>
              {resultCopy.markdown}
            </Tab>
          </TabList>
          <TabPanel id="body" className={styles.panel}>
            <BodyView
              result={entry.result}
              body={body}
              unit={unitForFormat(format)}
              extractCrops={entry.settings?.extractCrops ?? true}
              onTableCopied={onExported}
            />
          </TabPanel>
          <TabPanel id="tables" className={styles.panel}>
            <TablesView tables={entry.result.tables} unit={unitForFormat(format)} onCopied={onExported} />
          </TabPanel>
          <TabPanel id="visuals" className={styles.panel}>
            <VisualsView
              visuals={entry.result.visuals}
              unit={unitForFormat(format)}
              extractCrops={entry.settings?.extractCrops ?? true}
            />
          </TabPanel>
          <TabPanel id="chunks" className={styles.panel}>
            <ChunksView
              chunks={entry.result.chunks}
              unit={unitForFormat(format)}
              includeChunks={entry.settings?.includeChunks ?? true}
            />
          </TabPanel>
          <TabPanel id="markdown" className={styles.panel}>
            <pre className={styles.raw}>{entry.result.markdown}</pre>
          </TabPanel>
        </Tabs>
      ) : null}
    </div>
  );
}
