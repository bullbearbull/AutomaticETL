import type { TextChunk } from './api.ts';
import { resultCopy } from './copy.ts';
import type { PageUnit } from './formats.ts';
import { spanLabel } from './routing.ts';
import styles from './ChunksView.module.css';

/** More chunks than this are left to the ZIP, so the page stays quick. */
export const MAX_SHOWN_CHUNKS = 200;

export interface ChunksViewProps {
  chunks: readonly TextChunk[];
  unit: PageUnit;
  /** Whether the read made chunks at all (세부 설정). */
  includeChunks: boolean;
}

/** The AI용 문단 view: the text pieces made for AI tools, with where each one came from. */
export function ChunksView({ chunks, unit, includeChunks }: ChunksViewProps) {
  if (!includeChunks) return <p className={styles.empty}>{resultCopy.chunksOff}</p>;
  if (chunks.length === 0) return <p className={styles.empty}>{resultCopy.noChunks}</p>;
  const shown = chunks.slice(0, MAX_SHOWN_CHUNKS);
  return (
    <div className={styles.root}>
      {chunks.length > shown.length ? (
        <p className={styles.capped}>{resultCopy.chunksCapped(shown.length, chunks.length)}</p>
      ) : null}
      <ol className={styles.list}>
        {shown.map((chunk, index) => (
          <li key={chunk.id} className={styles.item}>
            <p className={`${styles.meta} tabular-nums`}>
              {resultCopy.chunkMeta(index + 1, spanLabel(chunk.pageStart, chunk.pageEnd, unit), chunk.charCount)}
            </p>
            <p className={styles.text}>{chunk.text}</p>
          </li>
        ))}
      </ol>
    </div>
  );
}
