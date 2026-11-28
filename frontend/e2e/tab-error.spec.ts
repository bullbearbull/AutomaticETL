import { appCopy, tabErrorCopy, tabLabels } from '../src/copy/common.ts';
import { startCopy as databaseStartCopy } from '../src/features/database/copy.ts';
import { openWithDocx, resultRegion } from './support/app.ts';
import { expectNoAxeViolations } from './support/axe.ts';
import { docxText } from './support/docx.ts';
import { expect, test } from './support/fixtures.ts';

/** The database tab's script and stylesheet. */
const DATABASE_FILES = '**/next/assets/DatabaseTab.*';

test('a tab that cannot load shows a notice in its own panel, and the other tabs keep their results', async ({
  page,
  pageLog,
}) => {
  await openWithDocx(page);
  const tabList = page.getByRole('tablist', { name: appCopy.tabListLabel });
  const panel = page.getByRole('tabpanel', { name: tabLabels.database, exact: true });

  // The tab's files cannot be fetched, as when AutoETL was closed.
  await page.route(DATABASE_FILES, (route) => route.abort());
  await tabList.getByRole('tab', { name: tabLabels.database }).click();
  const notice = panel.getByRole('alert');
  await expect(notice).toContainText(tabErrorCopy.notice.what);
  await expect(notice).toContainText(tabErrorCopy.notice.how);
  await expectNoAxeViolations(page);

  // The result the user already had is still there.
  await tabList.getByRole('tab', { name: tabLabels.extract }).click();
  await expect(resultRegion(page).getByText(docxText.paragraph)).toBeVisible();

  // Once the files can be fetched again, [다시 불러오기] opens the tab with its styles.
  await page.unroute(DATABASE_FILES);
  await tabList.getByRole('tab', { name: tabLabels.database }).click();
  await panel.getByRole('button', { name: tabErrorCopy.retry }).click();
  await expect(panel.getByRole('heading', { name: databaseStartCopy.title })).toBeVisible();
  await expect(panel.getByRole('alert')).toHaveCount(0);
  await expect(panel.locator(':scope > div')).toHaveCSS('flex-direction', 'column');

  // Both files were asked for under a new URL. Browsers without retriable
  // module loading, such as Safari and Chrome before 156, answer a module URL
  // that failed with the same failure, without a request.
  const retried = pageLog.requests.filter((url) => url.includes('/next/assets/DatabaseTab.') && url.endsWith('?retry=1'));
  expect([...new Set(retried.map((url) => new URL(url).pathname))].sort()).toEqual([
    '/next/assets/DatabaseTab.css',
    '/next/assets/DatabaseTab.js',
  ]);
});
