import { workspaceCopy } from '../src/copy/common.ts';
import { dropFiles, expectDocxRead, openWithDocx, resultRegion } from './support/app.ts';
import { DOCX_NAME, DROP_DOCX_NAME, docxFile } from './support/docx.ts';
import { expect, test } from './support/fixtures.ts';
import { APP_PATH, ORIGIN } from './support/server.ts';

test('a file dropped outside a drop zone leaves the screen as it was', async ({ page }, testInfo) => {
  await openWithDocx(page);

  const navigations: string[] = [];
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) navigations.push(frame.url());
  });
  // Runs after the app's own window listeners, so it sees whether one of them
  // stopped the browser from opening the file.
  await page.evaluate(() => {
    const seen: Record<string, boolean> = {};
    (window as unknown as { __dropSeen: typeof seen }).__dropSeen = seen;
    for (const type of ['dragover', 'drop']) {
      window.addEventListener(type, (event) => {
        seen[type] = event.defaultPrevented;
      });
    }
  });

  await dropFiles(page, page.getByRole('banner'), [docxFile(DROP_DOCX_NAME)], testInfo);

  const seen = await page.evaluate(() => (window as unknown as { __dropSeen: Record<string, boolean> }).__dropSeen);
  expect(seen).toEqual({ dragover: true, drop: true });

  expect(navigations).toEqual([]);
  expect(page.url()).toBe(`${ORIGIN}${APP_PATH}`);
  await expectDocxRead(page, DOCX_NAME);
  await expect(resultRegion(page).getByRole('heading', { level: 2, name: DOCX_NAME })).toBeVisible();
  await expect(page.getByRole('region', { name: workspaceCopy.list, exact: true })).not.toContainText(DROP_DOCX_NAME);
});
