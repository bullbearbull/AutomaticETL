import assert from 'node:assert/strict';
import { test } from 'node:test';
import { endsWithFinalConsonant, withParticle } from '../../src/lib/korean.ts';

test('withParticle picks the form that fits the last sound', () => {
  assert.equal(withParticle('표 1', '을/를'), '표 1을');
  assert.equal(withParticle('표 2', '을/를'), '표 2를');
  assert.equal(withParticle('표 10', '을/를'), '표 10을');
  assert.equal(withParticle('설정', '은/는'), '설정은');
  assert.equal(withParticle('엑셀·CSV로 DB 만들기', '은/는'), '엑셀·CSV로 DB 만들기는');
  assert.equal(withParticle('"보고서"', '이/가'), '"보고서"가');
  assert.equal(withParticle('HTML', '과/와'), 'HTML과');
});

test('endsWithFinalConsonant handles empty and symbol-only text', () => {
  assert.equal(endsWithFinalConsonant(''), false);
  assert.equal(endsWithFinalConsonant('   '), false);
  assert.equal(endsWithFinalConsonant('책)'), true);
});
