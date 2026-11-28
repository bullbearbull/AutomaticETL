/**
 * Korean particle helpers, so shared messages read naturally after any
 * name: `withParticle('표 2', '을/를')` → '표 2를'.
 */

export type ParticlePair = '을/를' | '은/는' | '이/가' | '과/와';

const HANGUL_FIRST = 0xac00;
const HANGUL_LAST = 0xd7a3;

/** Digits read in Sino-Korean: 영 일 이 삼 사 오 육 칠 팔 구. */
const DIGIT_HAS_FINAL = [true, true, false, true, false, false, true, true, true, false];

/** Latin letters whose English name ends in a consonant sound (L, M, N, R). */
const LATIN_HAS_FINAL = new Set(['l', 'm', 'n', 'r']);

/**
 * Whether the last readable character of `word` ends in a final consonant
 * (받침). Trailing spaces, quotes and closing brackets are skipped.
 */
export function endsWithFinalConsonant(word: string): boolean {
  const trimmed = word.replace(/[\s"'”’)\]}>»」』]+$/u, '');
  const last = trimmed.at(-1);
  if (last === undefined) return false;
  const code = last.codePointAt(0) ?? 0;
  if (code >= HANGUL_FIRST && code <= HANGUL_LAST) {
    return (code - HANGUL_FIRST) % 28 !== 0;
  }
  if (last >= '0' && last <= '9') {
    return DIGIT_HAS_FINAL[Number(last)] ?? false;
  }
  return LATIN_HAS_FINAL.has(last.toLowerCase());
}

/** Appends the particle form that fits `word`. */
export function withParticle(word: string, pair: ParticlePair): string {
  const [afterConsonant, afterVowel] = pair.split('/') as [string, string];
  return word + (endsWithFinalConsonant(word) ? afterConsonant : afterVowel);
}
