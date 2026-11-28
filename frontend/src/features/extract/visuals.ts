/**
 * Words for figures and charts (그림·차트): their kind, the image's alt
 * text, and why an image is missing.
 */
import type { VisualRegion } from './api.ts';
import { visualCopy } from './copy.ts';
import type { PageUnit } from './formats.ts';
import { whereLabel } from './routing.ts';

/** "그림", "도식·차트", … */
export function visualKindLabel(kind: string): string {
  return visualCopy.kinds[kind] ?? visualCopy.defaultKind;
}

/** The caption, or "2쪽의 그림" when the document gave none. */
export function visualAlt(visual: Pick<VisualRegion, 'caption' | 'page' | 'kind'>, unit: PageUnit): string {
  return visual.caption ?? visualCopy.alt(whereLabel(visual.page, unit), visualKindLabel(visual.kind));
}

/** Why there is no image to show, or `null` when there is one. */
export function missingImageText(
  visual: Pick<VisualRegion, 'imageDataUrl' | 'cropStatus'>,
  extractCrops: boolean,
): string | null {
  if (visual.imageDataUrl) return null;
  if (!extractCrops) return visualCopy.missing.off;
  switch (visual.cropStatus) {
    case 'saved':
    case 'embedded_extracted':
      // Kept in the ZIP but too large to send to the screen.
      return visualCopy.missing.tooLarge;
    case 'renderer_unavailable':
      return visualCopy.missing.renderer;
    default:
      return visualCopy.missing.failed;
  }
}
