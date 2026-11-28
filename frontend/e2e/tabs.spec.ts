import type { Page } from '@playwright/test';
import { appCopy, comingSoonCopy, tabIds, tabLabels, type TabId } from '../src/copy/common.ts';
import { startCopy as databaseStartCopy } from '../src/features/database/copy.ts';
import { startCopy } from '../src/features/extract/copy.ts';
import { openApp } from './support/app.ts';
import { expect, test } from './support/fixtures.ts';

async function expectSelected(page: Page, selected: TabId) {
  const tabList = page.getByRole('tablist', { name: appCopy.tabListLabel });
  for (const id of tabIds) {
    await expect(tabList.getByRole('tab', { name: tabLabels[id] })).toHaveAttribute(
      'aria-selected',
      id === selected ? 'true' : 'false',
    );
  }
  const tab = tabList.getByRole('tab', { name: tabLabels[selected] });
  await expect(tab).toBeFocused();

  // Exactly one panel is shown, and it belongs to the selected tab.
  const panel = page.getByRole('tabpanel', { name: tabLabels[selected], exact: true });
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute('aria-labelledby', (await tab.getAttribute('id')) ?? '');
  const startTitle = page.getByRole('heading', { name: startCopy.title });
  if (selected === 'extract') {
    await expect(panel.getByRole('heading', { name: startCopy.title })).toBeVisible();
  } else if (selected === 'database') {
    await expect(startTitle).toBeHidden();
    await expect(panel.getByRole('heading', { name: databaseStartCopy.title })).toBeVisible();
  } else {
    await expect(startTitle).toBeHidden();
    await expect(panel.getByRole('heading', { name: tabLabels[selected] })).toBeVisible();
    await expect(panel.getByText(comingSoonCopy.body)).toBeVisible();
  }
}

test('the four tabs move with arrow keys, Home and End', async ({ page }) => {
  await openApp(page);
  const tabList = page.getByRole('tablist', { name: appCopy.tabListLabel });
  await expect(tabList.getByRole('tab')).toHaveText(tabIds.map((id) => tabLabels[id]));

  await tabList.getByRole('tab', { name: tabLabels.extract }).focus();
  await expectSelected(page, 'extract');

  await page.keyboard.press('ArrowRight');
  await expectSelected(page, 'database');
  await page.keyboard.press('ArrowRight');
  await expectSelected(page, 'search');
  await page.keyboard.press('End');
  await expectSelected(page, 'settings');
  await page.keyboard.press('ArrowLeft');
  await expectSelected(page, 'search');
  await page.keyboard.press('Home');
  await expectSelected(page, 'extract');
});
