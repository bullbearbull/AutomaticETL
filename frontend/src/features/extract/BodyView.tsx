import { MarkdownView } from '../../components/index.ts';
import type { ParseResult } from './api.ts';
import type { BodyModel } from './body.ts';
import { resultCopy } from './copy.ts';
import type { PageUnit } from './formats.ts';
import { whereLabel } from './routing.ts';
import { ExtractTable } from './TablesView.tsx';
import { VisualFigure } from './VisualFigure.tsx';
import styles from './BodyView.module.css';

export interface BodyViewProps {
  result: ParseResult;
  body: BodyModel;
  unit: PageUnit;
  extractCrops: boolean;
  onTableCopied: () => void;
}

/**
 * The 본문 view: the document as a document. Text goes through
 * MarkdownView (no raw HTML); tables and figures are real components in
 * their place; a small "2쪽" mark shows where a page starts.
 */
export function BodyView({ result, body, unit, extractCrops, onTableCopied }: BodyViewProps) {
  if (body.segments.length === 0) return <p className={styles.empty}>{resultCopy.emptyBody}</p>;
  return (
    <div className={styles.body}>
      {body.segments.map((segment, position) => {
        switch (segment.kind) {
          case 'text':
            return <MarkdownView key={position} source={segment.markdown} className={styles.text} />;
          case 'page':
            return (
              <p key={position} className={styles.page}>
                {whereLabel(segment.page, unit)}
              </p>
            );
          case 'table': {
            const table = result.tables[segment.index];
            return table ? (
              <ExtractTable
                key={position}
                table={table}
                index={segment.index}
                caption={segment.caption}
                continued={segment.continued}
                onCopied={onTableCopied}
              />
            ) : null;
          }
          case 'visual': {
            const visual = result.visuals[segment.index];
            return visual ? (
              <VisualFigure key={position} visual={visual} unit={unit} extractCrops={extractCrops} />
            ) : null;
          }
          default:
            return null;
        }
      })}
    </div>
  );
}
