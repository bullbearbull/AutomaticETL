import { dropZoneCopy, workspaceCopy } from '../src/copy/common.ts';
import { listCopy, statusText, unitCopy } from '../src/features/extract/copy.ts';
import { dropFiles, expectDocxRead, openWithDocx, resultRegion, statusLine } from './support/app.ts';
import { DOCX_NAME, DROP_DOCX_NAME, docxFile } from './support/docx.ts';
import { expect, test } from './support/fixtures.ts';
import { APP_PATH, ORIGIN } from './support/server.ts';

const STRAY_NAME = 'stray.docx';
/** What the file list shows under the fixture DOCX once it is read. */
const listMeta = `${unitCopy.page.count(1)} · ${listCopy.tables(1)}`;

test('a file dropped outside a drop zone leaves the screen as it was', async ({ page }, testInfo) => {
  await openWithDocx(page);
  const banner = page.getByRole('banner');
  const list = page.getByRole('region', { name: workspaceCopy.list, exact: true });

  // Headless Chromium never opens a dropped file, so check the events
  // themselves: a file drag over or onto the top bar must be cancelled, or a
  // real browser would replace the app with the file.
  const outcome = await banner.evaluate((element, name) => {
    const fire = (type: 'dragover' | 'drop') => {
      const dataTransfer = new DataTransfer();
      dataTransfer.items.add(new File(['x'], name));
      const event = new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer });
      const notCancelled = element.dispatchEvent(event);
      return { notCancelled, dropEffect: dataTransfer.dropEffect };
    };
    return { dragover: fire('dragover'), drop: fire('drop') };
  }, STRAY_NAME);
  expect(outcome.dragover).toEqual({ notCancelled: false, dropEffect: 'none' });
  expect(outcome.drop.notCancelled).toBe(false);

  const navigations: string[] = [];
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) navigations.push(frame.url());
  });
  await dropFiles(page, banner, [docxFile(STRAY_NAME)], testInfo);

  expect(navigations).toEqual([]);
  expect(page.url()).toBe(`${ORIGIN}${APP_PATH}`);
  await expectDocxRead(page, DOCX_NAME);
  await expect(resultRegion(page).getByRole('heading', { level: 2, name: DOCX_NAME })).toBeVisible();
  await expect(list).not.toContainText(STRAY_NAME);

  // The guard leaves real drop zones alone: a drop on the list's zone is read.
  await dropFiles(page, list.getByText(dropZoneCopy.compactHint), [docxFile(DROP_DOCX_NAME)], testInfo);
  await expect(statusLine(page, statusText.doneAll(2))).toBeVisible();
  const options = list.getByRole('option');
  await expect(options).toHaveCount(2);
  await expect(options.nth(0)).toHaveAccessibleName(listCopy.option(DOCX_NAME, listMeta));
  await expect(options.nth(1)).toHaveAccessibleName(listCopy.option(DROP_DOCX_NAME, listMeta));
  await expect(list).not.toContainText(STRAY_NAME);
});
