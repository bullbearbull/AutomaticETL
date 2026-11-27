import { Details } from '../../components/index.ts';
import type { VisualRegion } from './api.ts';
import { resultCopy, visualCopy } from './copy.ts';
import type { PageUnit } from './formats.ts';
import { whereLabel } from './routing.ts';
import { missingImageText, visualAlt, visualKindLabel } from './visuals.ts';
import styles from './VisualFigure.module.css';

export interface VisualFigureProps {
  visual: VisualRegion;
  unit: PageUnit;
  /** Whether the read kept figure images (세부 설정). */
  extractCrops: boolean;
  /** Show the server's reason under 자세히 (그림·차트 view). */
  showReason?: boolean;
}

/** One figure or chart: its image (or why there is none), caption and place. */
export function VisualFigure({ visual, unit, extractCrops, showReason = false }: VisualFigureProps) {
  const kind = visualKindLabel(visual.kind);
  const missing = missingImageText(visual, extractCrops);
  return (
    <figure className={styles.figure}>
      {visual.imageDataUrl ? (
        <img src={visual.imageDataUrl} alt={visualAlt(visual, unit)} className={styles.image} />
      ) : (
        <p className={styles.missing}>{missing}</p>
      )}
      <figcaption className={styles.caption}>
        {visual.caption ? <span className={styles.captionText}>{visual.caption}</span> : null}
        <span className={styles.meta}>{visualCopy.meta(whereLabel(visual.page, unit), kind)}</span>
      </figcaption>
      {showReason && visual.reason ? (
        <Details summary={visualCopy.reason} className={styles.reason}>
          <p className={styles.reasonText}>{visual.reason}</p>
        </Details>
      ) : null}
    </figure>
  );
}

export interface VisualsViewProps {
  visuals: readonly VisualRegion[];
  unit: PageUnit;
  extractCrops: boolean;
}

/** The 그림·차트 view: every figure and chart in the file. */
export function VisualsView({ visuals, unit, extractCrops }: VisualsViewProps) {
  if (visuals.length === 0) return <p className={styles.empty}>{resultCopy.noVisuals}</p>;
  return (
    <ul className={styles.list}>
      {visuals.map((visual) => (
        <li key={visual.id} className={styles.item}>
          <VisualFigure visual={visual} unit={unit} extractCrops={extractCrops} showReason />
        </li>
      ))}
    </ul>
  );
}
