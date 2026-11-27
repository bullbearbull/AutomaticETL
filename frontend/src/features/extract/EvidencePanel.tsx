import { Details } from '../../components/index.ts';
import { evidenceCopy, formatCopy } from './copy.ts';
import { errorAction } from './errors.ts';
import { foundSummary, methodSentences } from './routing.ts';
import type { ExtractEntry } from './state.ts';
import styles from './EvidencePanel.module.css';

/** Whether the 근거 column has something to say for this file. */
export function hasEvidence(entry: ExtractEntry): boolean {
  if (entry.status !== 'error') return true;
  return entry.error !== null && errorAction(entry.error.kind) === 'pickOther';
}

/**
 * The 근거 column, "어떻게 읽었나요": how each page was read in plain words,
 * what was found, notes, and the raw processing record under 자세히. Next
 * to a file the tab cannot read, it lists the files it can.
 */
export function EvidencePanel({ entry }: { entry: ExtractEntry }) {
  if (entry.status === 'error') {
    return (
      <div className={styles.root}>
        <h2 className={styles.title}>{evidenceCopy.supportedTitle}</h2>
        <ul className={styles.plainList}>
          {formatCopy.supportedItems.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
        <p className={styles.note}>{formatCopy.sizeLimit}</p>
      </div>
    );
  }

  if (entry.status !== 'done' || !entry.result) {
    return (
      <div className={styles.root}>
        <h2 className={styles.title}>{evidenceCopy.title}</h2>
        <p className={styles.note}>{evidenceCopy.pending}</p>
      </div>
    );
  }

  const { result } = entry;
  const sentences = methodSentences(result.pages, result.document.format);
  return (
    <div className={styles.root}>
      <h2 className={styles.title}>{evidenceCopy.title}</h2>
      {sentences.length > 0 ? (
        <section className={styles.section}>
          <h3 className={styles.subtitle}>{evidenceCopy.method}</h3>
          <ul className={styles.plainList}>
            {sentences.map((sentence) => (
              <li key={sentence}>{sentence}</li>
            ))}
          </ul>
        </section>
      ) : null}
      <section className={styles.section}>
        <h3 className={styles.subtitle}>{evidenceCopy.found}</h3>
        <p>{foundSummary(result, entry.settings?.includeChunks ?? true)}</p>
      </section>
      {result.warnings.length > 0 ? (
        <section className={styles.section}>
          <h3 className={styles.subtitle}>{evidenceCopy.notes}</h3>
          <p>{evidenceCopy.notesLead(result.warnings.length)}</p>
          <Details summary={evidenceCopy.notesDetails}>
            <ul className={styles.rawList}>
              {result.warnings.map((warning, index) => (
                <li key={index}>{warning}</li>
              ))}
            </ul>
          </Details>
        </section>
      ) : null}
      {result.routingReport ? (
        <Details summary={evidenceCopy.report}>
          <pre className={styles.report}>{result.routingReport}</pre>
        </Details>
      ) : null}
    </div>
  );
}
