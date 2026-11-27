import { useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react';
import { StatusBar, StepIndicator, Workspace } from '../../components/index.ts';
import { buildBody } from './body.ts';
import { EvidencePanel, hasEvidence } from './EvidencePanel.tsx';
import { ExportBar } from './ExportBar.tsx';
import { FileList } from './FileList.tsx';
import { checkExtractFile, formatFromName, unitForFormat } from './formats.ts';
import { readEntry } from './reader.ts';
import { ResultView, type ResultViewId } from './ResultView.tsx';
import { DEFAULT_SETTINGS, sameReadSettings, type ExtractSettings } from './settings.ts';
import { SettingsPanel } from './SettingsPanel.tsx';
import { StartScreen } from './StartScreen.tsx';
import {
  extractReducer,
  initialState,
  isZipExpired,
  nextQueued,
  selectedEntry,
  statusFor,
  stepFor,
} from './state.ts';
import styles from './ExtractTab.module.css';

type FocusTarget = 'result' | 'start';

/**
 * Entry of the "문서 글·표 추출" tab (App.tsx loads it lazily, so this folder
 * builds into assets/ExtractTab.js and .css).
 *
 * Files are read as soon as they are dropped or picked, one at a time, one
 * POST /api/parse each. [다시 읽기] after a settings change reads only the
 * shown file again; files added later use the settings of the moment they
 * start.
 */
export default function ExtractTab() {
  const [state, dispatch] = useReducer(extractReducer, initialState);
  const [settings, setSettings] = useState<ExtractSettings>(DEFAULT_SETTINGS);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [view, setView] = useState<ResultViewId>('body');

  const nextId = useRef(0);
  const running = useRef<{ id: string; controller: AbortController } | null>(null);
  const resultHeading = useRef<HTMLHeadingElement>(null);
  const startHeading = useRef<HTMLHeadingElement>(null);
  const pendingFocus = useRef<FocusTarget | null>(null);
  const lastFocused = useRef<Element | null>(null);

  const selected = selectedEntry(state);
  const status = statusFor(state);

  // Read the next waiting file once nothing else is being read.
  useEffect(() => {
    if (running.current) return;
    const next = nextQueued(state);
    if (!next) return;
    const controller = new AbortController();
    running.current = { id: next.id, controller };
    const used = settings;
    dispatch({ type: 'start', id: next.id, settings: used, now: Date.now() });
    void readEntry(next, used, controller.signal).then((outcome) => {
      // Free the slot before the state change, so the next file starts right after it.
      if (running.current?.controller === controller) running.current = null;
      if (outcome.type === 'succeed') {
        dispatch({ type: 'succeed', id: next.id, result: outcome.result, now: Date.now() });
      } else if (outcome.type === 'fail') {
        dispatch({ type: 'fail', id: next.id, error: outcome.error, now: Date.now() });
      }
    });
  }, [state, settings]);

  // Stop a read in progress when the tab goes away.
  useEffect(() => () => running.current?.controller.abort(), []);

  // Move focus where the user expects it after a change that removed the
  // focused control; and never leave focus on <body>.
  useLayoutEffect(() => {
    const target = pendingFocus.current;
    if (target !== null) {
      pendingFocus.current = null;
      (target === 'result' ? resultHeading.current : startHeading.current)?.focus();
      return;
    }
    const last = lastFocused.current;
    const active = document.activeElement;
    if (last && !last.isConnected && (active === null || active === document.body)) {
      (resultHeading.current ?? startHeading.current)?.focus();
    }
  });

  const addFiles = (files: File[], focus?: FocusTarget) => {
    if (files.length === 0) return;
    if (focus) pendingFocus.current = focus;
    dispatch({
      type: 'add',
      files: files.map((file) => {
        nextId.current += 1;
        return { id: `file-${nextId.current}`, file, name: file.name, size: file.size, error: checkExtractFile(file) };
      }),
    });
  };

  const removeEntry = (id: string) => {
    if (running.current?.id === id) {
      running.current.controller.abort();
      running.current = null;
    }
    pendingFocus.current = state.entries.length > 1 ? 'result' : 'start';
    dispatch({ type: 'remove', id });
  };

  const rereadEntry = (id: string) => {
    pendingFocus.current = 'result';
    dispatch({ type: 'retry', id });
  };

  const result = selected?.result ?? null;
  const selectedName = selected?.name;
  const body = useMemo(() => {
    if (!result) return null;
    return buildBody({
      markdown: result.markdown,
      tables: result.tables,
      visuals: result.visuals,
      unit: unitForFormat(result.document.format || formatFromName(selectedName ?? '')),
      ...(selectedName !== undefined ? { fileName: selectedName } : {}),
    });
  }, [result, selectedName]);

  const settingsPanel = (
    <SettingsPanel
      settings={settings}
      onChange={setSettings}
      isExpanded={settingsOpen}
      onExpandedChange={setSettingsOpen}
    />
  );

  const rereadOffer =
    selected?.status === 'done' && selected.settings && !sameReadSettings(selected.settings, settings)
      ? { name: selected.name, onReread: () => rereadEntry(selected.id) }
      : null;

  return (
    <div
      className={styles.root}
      onFocus={(event) => {
        lastFocused.current = event.target;
      }}
    >
      <StepIndicator current={stepFor(state)} />

      {selected ? (
        <Workspace
          list={
            <FileList
              entries={state.entries}
              selectedId={state.selectedId}
              onSelect={(id) => dispatch({ type: 'select', id })}
              onFiles={(files) => addFiles(files)}
              settings={settingsPanel}
              reread={rereadOffer}
            />
          }
          result={
            <ResultView
              entry={selected}
              body={body}
              view={view}
              onViewChange={setView}
              headingRef={resultHeading}
              onPickOther={(files) => addFiles(files, 'result')}
              onRemove={() => removeEntry(selected.id)}
              onRetry={() => rereadEntry(selected.id)}
              onExported={() => dispatch({ type: 'exported', id: selected.id })}
            />
          }
          evidence={hasEvidence(selected) ? <EvidencePanel entry={selected} /> : null}
          exportBar={
            selected.status === 'done' && selected.result && body ? (
              <ExportBar
                key={selected.id}
                result={selected.result}
                extractCrops={selected.settings?.extractCrops ?? true}
                includeChunks={selected.settings?.includeChunks ?? true}
                copyText={body.copyText}
                expired={isZipExpired(state, selected)}
                onExported={() => dispatch({ type: 'exported', id: selected.id })}
                onZipFetched={() => dispatch({ type: 'zipFetched', id: selected.id })}
                onReread={() => rereadEntry(selected.id)}
              />
            ) : undefined
          }
          status={null}
        />
      ) : (
        <StartScreen headingRef={startHeading} onFiles={(files) => addFiles(files, 'result')} settings={settingsPanel} />
      )}

      {/* One status line for both screens, so its live region stays in place and every change is read out. */}
      <div className={styles.status}>
        <StatusBar message={status.message} tone={status.tone} startedAt={status.startedAt} endedAt={status.endedAt} />
      </div>
    </div>
  );
}
