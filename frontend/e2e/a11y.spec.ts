import type { Page } from '@playwright/test';
import { errorCopy } from '../src/copy/common.ts';
import { expectNoAxeViolations } from './support/axe.ts';
import { openApp, openWithDocx, pickFile, resultRegion } from './support/app.ts';
import { xlsFile } from './support/docx.ts';
import { expect, test } from './support/fixtures.ts';

/** The page does not scroll sideways: only wide tables scroll, inside their own box. */
async function expectNoSidewaysScroll(page: Page) {
  const { scrollWidth, viewportWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    viewportWidth: window.innerWidth,
  }));
  expect(scrollWidth).toBeLessThanOrEqual(viewportWidth);
}

test.describe('axe finds no WCAG 2.2 AA violations', () => {
  test('on the start screen', async ({ page }) => {
    await openApp(page);
    await expectNoAxeViolations(page);
  });

  test('on the result screen', async ({ page }) => {
    await openWithDocx(page);
    await expectNoAxeViolations(page);
  });

  test('on the error screen', async ({ page }) => {
    await openApp(page);
    await pickFile(page, xlsFile());
    await expect(resultRegion(page).getByRole('alert')).toContainText(errorCopy.unsupportedFile.what);
    await expectNoAxeViolations(page);
  });
});

test.describe('at 320px wide', () => {
  test.use({ viewport: { width: 320, height: 640 } });

  test('the start screen does not scroll sideways', async ({ page }) => {
    await openApp(page);
    await expectNoSidewaysScroll(page);
  });

  test('the result screen has no axe violations and does not scroll sideways', async ({ page }) => {
    await openWithDocx(page);
    // The workspace (the result column's parent) is in its one-column layout.
    await expect(resultRegion(page).locator('xpath=..')).toHaveAttribute('data-layout', 'narrow');
    await expectNoAxeViolations(page);
    await expectNoSidewaysScroll(page);
  });
});
