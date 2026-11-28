import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import type { Locator, Page } from '@playwright/test';
import { appCopy, buttonCopy, errorCopy, tabLabels, workspaceCopy } from '../src/copy/common.ts';
import {
  exportCopy,
  piiDialogCopy,
  purposeCopy,
  startCopy,
  validationCopy,
} from '../src/features/database/copy.ts';
import { openApp } from './support/app.ts';
import { expectNoAxeViolations } from './support/axe.ts';
import { expect, test } from './support/fixtures.ts';

function csvPayload(name: string, content: string) {
  return { name, mimeType: 'text/csv', buffer: Buffer.from(content, 'utf-8') };
}

const BRANCHES_CSV = csvPayload(
  'branches.csv',
  'branch_code,branch_name\nB01,강남점\nB02,서초점\n',
);

const CUSTOMERS_CSV = csvPayload(
  'customers.csv',
  'customer_id,customer_name,phone,email,grade\n' +
    '101,김민수,010-1234-5678,minsu@example.com,VIP\n' +
    '102,이서연,010-9876-5432,seoyeon@example.com,일반\n',
);

const ORDERS_CSV = csvPayload(
  'orders.csv',
  'order_id,customer_id,order_date,amount\n' +
    '1001,101,2026-11-01,12000\n' +
    '1002,102,2026-11-02,24000\n',
);

async function openDatabaseTab(page: Page): Promise<Locator> {
  await openApp(page);
  const tabList = page.getByRole('tablist', { name: appCopy.tabListLabel });
  await tabList.getByRole('tab', { name: tabLabels.database }).click();
  const panel = page.getByRole('tabpanel', { name: tabLabels.database, exact: true });
  await expect(panel.getByRole('heading', { level: 2, name: startCopy.title })).toBeVisible();
  return panel;
}

async function pickDatabaseFiles(panel: Locator, page: Page, files: ReturnType<typeof csvPayload>[]) {
  const chooser = page.waitForEvent('filechooser');
  await panel.getByRole('button', { name: buttonCopy.pickFiles }).click();
  await (await chooser).setFiles(files);
}

async function expectNoSidewaysScroll(page: Page) {
  const { scrollWidth, viewportWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    viewportWidth: window.innerWidth,
  }));
  expect(scrollWidth).toBeLessThanOrEqual(viewportWidth);
}

test('auto-analyzes CSVs, re-validates inline edits, and masks PII via ConfirmDialog before saving SQLite', async ({
  page,
}, testInfo) => {
  const panel = await openDatabaseTab(page);
  await expectNoAxeViolations(page);

  await pickDatabaseFiles(panel, page, [BRANCHES_CSV, CUSTOMERS_CSV, ORDERS_CSV]);

  // Immediate analysis without a separate "표 구조 분석하기" or "변경 내용 반영" button.
  await expect(panel.getByRole('button', { name: exportCopy.saveDb })).toBeVisible({ timeout: 20_000 });
  await expect(panel.getByRole('button', { name: /변경 내용 반영/ })).toHaveCount(0);

  // Step 2 active.
  await expect(panel.locator('[aria-current="step"]')).toContainText('확인·다듬기');

  // Switch to customers table and verify PII evidence reasons + masked previews.
  const listRegion = panel.getByRole('region', { name: workspaceCopy.list, exact: true });
  await listRegion.getByRole('option', { name: /customers/i }).click();

  const evidenceRegion = panel.getByRole('region', { name: workspaceCopy.evidence, exact: true });
  await expect(evidenceRegion).toContainText('찾은 이유');
  await expect(evidenceRegion).toContainText('김*수');
  await expect(evidenceRegion).toContainText('010-****-5678');

  // Invalid DB column name triggers accessible inline guidance; valid name auto-revalidates via session_id.
  const result = panel.getByRole('region', { name: workspaceCopy.result, exact: true });
  const gradeInput = result.getByRole('textbox', { name: 'grade의 DB 열 이름' });
  await gradeInput.fill('2bad');
  await expect(result.getByText(validationCopy.invalidIdentifier)).toBeVisible();

  const revalReq = page.waitForRequest(
    (req) =>
      req.url().endsWith('/api/analyze-tables') &&
      req.method() === 'POST' &&
      (req.postData() ?? '').includes('session_id'),
  );
  await gradeInput.fill('customer_tier');
  await gradeInput.press('Enter');
  const req = await revalReq;
  expect(req.postData() ?? '').toContain('"target_column":"customer_tier"');

  await expect(result.getByRole('textbox', { name: 'grade의 DB 열 이름' })).toHaveValue('customer_tier');
  await expectNoAxeViolations(page);

  // Save with unmasked PII -> ConfirmDialog opens, primary focus on [가리고 저장하기], Esc cancels.
  await panel.getByRole('button', { name: exportCopy.saveDb }).click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(piiDialogCopy.title(3));
  const maskSaveBtn = dialog.getByRole('button', { name: piiDialogCopy.primaryAction });
  await expect(maskSaveBtn).toBeFocused();
  await expectNoAxeViolations(page);

  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(panel.locator('[aria-current="step"]')).toContainText('확인·다듬기');

  // Click [DB 파일로 저장하기] again and confirm [가리고 저장하기] -> downloads real SQLite file.
  await panel.getByRole('button', { name: exportCopy.saveDb }).click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('alertdialog').getByRole('button', { name: piiDialogCopy.primaryAction }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('autoetl_data.sqlite');

  const sqlitePath = testInfo.outputPath('downloaded.sqlite');
  await download.saveAs(sqlitePath);
  const bytes = await readFile(sqlitePath);
  expect(bytes.subarray(0, 15).toString('ascii')).toBe('SQLite format 3');

  // Inspect actual SQLite rows with Python sqlite3 to verify masking AND column rename persisted.
  const dumped = execFileSync(
    'python3',
    [
      '-c',
      'import json, sqlite3, sys; c=sqlite3.connect(sys.argv[1]); cols=[d[0] for d in c.execute("SELECT customer_name, phone, customer_tier FROM customers ORDER BY customer_id").description or []]; rows=c.execute("SELECT customer_name, phone, customer_tier FROM customers ORDER BY customer_id").fetchall(); print(json.dumps(rows, ensure_ascii=False))',
      sqlitePath,
    ],
    { encoding: 'utf-8' },
  );
  const rows = JSON.parse(dumped) as string[][];
  expect(rows).toEqual([
    ['김*수', '010-****-5678', 'VIP'],
    ['이*연', '010-****-5432', '일반'],
  ]);

  // Step 3 active and "저장 결과" view shows masked columns.
  await expect(panel.locator('[aria-current="step"]')).toContainText('내보내기');
  await expect(result).toContainText('customer_name');
  await expect(result).toContainText('phone');
});

test('archive purpose shows inline notice immediately and skips the PII confirmation dialog', async ({
  page,
}) => {
  const panel = await openDatabaseTab(page);

  // Selecting "원본 보관용" shows immediate inline warning on start screen.
  await panel.getByText('원본 보관용', { exact: true }).click();
  await expect(panel.getByRole('radio', { name: /원본 보관용/ })).toBeChecked();
  await expect(panel.getByText(purposeCopy.archiveNotice)).toBeVisible();

  await pickDatabaseFiles(panel, page, [CUSTOMERS_CSV]);
  await expect(panel.getByRole('button', { name: exportCopy.saveDb })).toBeVisible({ timeout: 20_000 });
  await expect(panel.getByText(purposeCopy.archiveNotice)).toBeVisible();

  // Saving directly skips ConfirmDialog and starts DB download.
  const downloadPromise = page.waitForEvent('download');
  await panel.getByRole('button', { name: exportCopy.saveDb }).click();
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('autoetl_data.sqlite');
});

test('rejects legacy .xls locally with actionable guidance and keeps axe clean on error', async ({
  page,
  pageLog,
}) => {
  const panel = await openDatabaseTab(page);
  const beforeRequests = pageLog.requests.length;

  await pickDatabaseFiles(panel, page, [
    { name: 'legacy_data.xls', mimeType: 'application/vnd.ms-excel', buffer: Buffer.from('xls') },
  ]);

  const alert = panel.getByRole('region', { name: workspaceCopy.result, exact: true }).getByRole('alert');
  await expect(alert).toContainText(errorCopy.unsupportedFile.what);
  await expect(alert).toContainText(errorCopy.unsupportedFile.xlsWhy);
  expect(
    pageLog.requests.slice(beforeRequests).filter((u) => u.includes('/api/analyze-tables')),
  ).toEqual([]);
  await expectNoAxeViolations(page);
});

test.describe('database tab at 320px wide', () => {
  test.use({ viewport: { width: 320, height: 640 } });

  test('has no sideways scroll or axe violations on start and result screens', async ({ page }) => {
    const panel = await openDatabaseTab(page);
    await expectNoSidewaysScroll(page);
    await expectNoAxeViolations(page);

    await pickDatabaseFiles(panel, page, [CUSTOMERS_CSV]);
    await expect(panel.getByRole('button', { name: exportCopy.saveDb })).toBeVisible({ timeout: 20_000 });
    await expectNoSidewaysScroll(page);
    await expectNoAxeViolations(page);
  });
});
