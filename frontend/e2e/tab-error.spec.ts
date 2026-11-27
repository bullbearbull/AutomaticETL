import { appCopy, comingSoonCopy, tabErrorCopy, tabLabels } from '../src/copy/common.ts';
import { openWithDocx, resultRegion } from './support/app.ts';
import { expectNoAxeViolations } from './support/axe.ts';
import { docxText } from './support/docx.ts';
import { expect, test } from './support/fixtures.ts';

const DATABASE_CHUNK = '**/next/assets/DatabaseTab.js';

test('a tab that cannot load shows a notice in its own panel, and the other tabs keep their results', async ({ page }) => {
  await openWithDocx(page);
  const tabList = page.getByRole('tablist', { name: appCopy.tabListLabel });
  const panel = page.getByRole('tabpanel', { name: tabLabels.database, exact: true });

  // The tab's file cannot be fetched, as when AutoETL was closed.
  await page.route(DATABASE_CHUNK, (route) => route.abort());
  await tabList.getByRole('tab', { name: tabLabels.database }).click();
  const notice = panel.getByRole('alert');
  await expect(notice).toContainText(tabErrorCopy.notice.what);
  await expect(notice).toContainText(tabErrorCopy.notice.how);
  await expectNoAxeViolations(page);

  // The result the user already had is still there.
  await tabList.getByRole('tab', { name: tabLabels.extract }).click();
  await expect(resultRegion(page).getByText(docxText.paragraph)).toBeVisible();

  // Once the file can be fetched again, [다시 불러오기] opens the tab.
  await page.unroute(DATABASE_CHUNK);
  await tabList.getByRole('tab', { name: tabLabels.database }).click();
  await panel.getByRole('button', { name: tabErrorCopy.retry }).click();
  await expect(panel.getByText(comingSoonCopy.body)).toBeVisible();
  await expect(panel.getByRole('alert')).toHaveCount(0);
});
