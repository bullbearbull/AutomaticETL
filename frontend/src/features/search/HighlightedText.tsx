import { splitTextWithHighlights } from './highlight.ts';
import styles from './HighlightedText.module.css';

export interface HighlightedTextProps {
  text: string;
  terms: readonly string[];
}

/**
 * Renders plain text with matched search terms wrapped in `<mark>` styled strictly
 * with bold weight + underline (no background color), matching monochrome design rules.
 */
export function HighlightedText({ text, terms }: HighlightedTextProps) {
  const segments = splitTextWithHighlights(text, terms);
  return (
    <>
      {segments.map((seg, idx) =>
        seg.match ? (
          <mark key={idx} className={styles.mark}>
            {seg.text}
          </mark>
        ) : (
          <span key={idx}>{seg.text}</span>
        ),
      )}
    </>
  );
}
